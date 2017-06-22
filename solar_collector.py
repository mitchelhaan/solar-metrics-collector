#!/usr/bin/env python2

import collections
import datetime
import json
import logging
import numpy
import requests
import time

# ADC support
import Adafruit_GPIO.SPI as SPI
import Adafruit_MCP3008

# EPSolar Tracer support
from pyepsolartracer.client import EPsolarTracerClient
from pymodbus.client.sync import ModbusSerialClient as ModbusClient


# Constants
api_endpoint = 'https://example.com/api/solar/upload'
api_auth = ('username', 'password')
failed_upload_file = "/opt/solar_upload_failed.json"

# Collect every 5 seconds, aggregate and upload once per minute during the day, once per 10 mins at night
collection_interval_sec = 5.0
day_upload_interval_sec = 1.0 * 60
night_upload_interval_sec = 10.0 * 60

# Measuring load after the inverter, but there are a few things on the DC side too
pre_inverter_load = 42

# Global variables
is_daytime = False
solar_client = EPsolarTracerClient(serialclient=ModbusClient(method='rtu', port='/dev/ttyUSB0', baudrate=115200))
adc_spidev = SPI.SpiDev(0, 0, 3000000)

logging.basicConfig()
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


class MetricsCollection:
    """Simple class to collect and average metrics"""

    # Special cases for certain metrics
    _most_recent = ['timestamp', 'kwh_today', 'kwh_total']
    _most_common = ['pv_charging_mode']

    def __init__(self):
        self._metrics = dict()

    # Append each metric to its respective collection
    def add(self, new_metrics):
        for metric, val in new_metrics.items():
            if metric not in self._metrics:
                self._metrics[metric] = list()
            self._metrics[metric].append(val)

    # Roll up the collected metrics (average by default)
    def aggregate(self):
        aggregated_stats = dict()
        for key in self._metrics.keys():
            if key in self._most_recent:
                aggregated_stats[key] = self._metrics[key][-1]
            elif key in self._most_common:
                aggregated_stats[key] = collections.Counter(self._metrics[key]).most_common()[0][0]
            else:
                # Round all averages to 2 decimal places to keep things simple
                aggregated_stats[key] = round(numpy.mean(numpy.array(self._metrics[key])), 2)
        return aggregated_stats

    def clear(self):
        self._metrics.clear()


def status_loop():
    """Main loop, runs continuously gathering metrics"""

    solar_client.connect()

    metrics = MetricsCollection()
    next_upload_time = 0

    while True:
        loop_start = time.time()

        # Update more frequently during the day because there is more interesting data
        update_daytime_state()
        upload_interval_sec = day_upload_interval_sec if is_daytime else night_upload_interval_sec

        metrics.add(get_current_metrics())

        log.debug("Finished collection loop in %f seconds", time.time() - loop_start)

        if time.time() > next_upload_time:
            log.info("Uploading aggregated metrics")

            upload_metrics(metrics.aggregate())
            metrics.clear()

            delay = upload_interval_sec - time.time() % upload_interval_sec
            next_upload_time = time.time() + delay

        delay = collection_interval_sec - time.time() % collection_interval_sec
        log.debug("Next collection scheduled in %f seconds", delay)
        log.debug("Next upload scheduled in %f seconds", next_upload_time - time.time())
        time.sleep(delay)


def update_daytime_state():
    """Update the day/night state from the solar controller
    
    Ideally this would simply be reading the day/night value, but my controller doesn't seem to support it
    """

    global is_daytime

    day_night = solar_client.read_input("Day/Night")

    if day_night.value is not None:
        log.debug("day_night = %d", int(day_night))
        # Day = 0, Night = 1
        is_daytime = int(day_night) == 0
    else:
        log.debug("No value for Day/Night, calculating manually")
        day_voltage = float(solar_client.read_input("Day Time Threshold Volt.(DTTV)"))
        night_voltage = float(solar_client.read_input("Night Time Threshold Volt.(NTTV)"))
        current_voltage = float(solar_client.read_input("Charging equipment input voltage"))
        log.debug("Day: %f  Night: %f  Current: %f", day_voltage, night_voltage, current_voltage)

        # There should be a gap between day/night voltage to allow hysteresis
        if current_voltage <= night_voltage:
            is_daytime = False
        if current_voltage >= day_voltage:
            is_daytime = True


def get_current_metrics():
    """Collect all the metrics for this instant"""

    log.debug("Getting current metrics")

    current_metrics = dict()

    # Solar panel metrics
    current_metrics['pv_volts'] = float(solar_client.read_input("Charging equipment input voltage"))
    current_metrics['pv_amps'] = float(solar_client.read_input("Charging equipment input current"))
    current_metrics['pv_watts'] = float(solar_client.read_input("Charging equipment input power"))

    # Power collection metrics
    current_metrics['kwh_today'] = float(solar_client.read_input("Generated energy today"))
    current_metrics['kwh_total'] = float(solar_client.read_input("Total generated energy"))

    # Battery bank metrics
    pv_charging_mode_raw = (int(solar_client.read_input("Charging equipment status")) & 0x000C) >> 2

    # Convert the charging mode bit-field to a string
    if pv_charging_mode_raw == 1:
        current_metrics['pv_charging_mode'] = "Float"
    elif pv_charging_mode_raw == 2:
        current_metrics['pv_charging_mode'] = "MPPT"
    elif pv_charging_mode_raw == 3:
        current_metrics['pv_charging_mode'] = "Equalization"
    else:
        current_metrics['pv_charging_mode'] = "Not charging"

    current_metrics['battery_volts'] = float(solar_client.read_input("Charging equipment output voltage"))
    current_metrics['battery_amps'] = get_avg_battery_current()
    current_metrics['battery_watts'] = current_metrics['battery_volts'] * current_metrics['battery_amps']

    current_metrics['battery_temp'] = float(solar_client.read_input("Battery Temp."))

    # Calculate DC load watts by the difference of solar input and average battery power
    current_metrics['dc_load_watts'] = current_metrics['pv_watts'] - current_metrics['battery_watts']

    # We're not measuring AC voltage, just assuming 120V RMS and power factor of 1.0
    current_metrics['ac_load_watts'] = get_ac_rms_current() * 120.0

    current_metrics['load_watts'] = current_metrics['ac_load_watts'] + pre_inverter_load

    current_metrics['timestamp'] = datetime.datetime.now()

    log.info('Solar: %.2fW DC Load: %.2fW AC Load: %.2fW', current_metrics['pv_watts'], current_metrics['dc_load_watts'], current_metrics['ac_load_watts'])

    return current_metrics


def get_avg_battery_current():
    """Calculate the average amps, because the current is 120Hz pulsed from DC->AC conversion"""

    amps_adc = 4
    amps_zero = 511  # 10-bit ADC, so 0-1023 full scale
    amps_scale = 0.114  # (5 V / 1024 div) / 43 mV/A

    mcp = Adafruit_MCP3008.MCP3008(spi=adc_spidev)

    start_time = time.time()
    battery_amps = list()

    # Collect a decent amount of samples, should be an even division of 120Hz
    while time.time() - start_time < 0.5:
        battery_amps.append(mcp.read_adc(amps_adc))

    b = (amps_zero - numpy.array(battery_amps)) * amps_scale
    b_avg = numpy.mean(b)

    if log.isEnabledFor(logging.DEBUG):
        b_min = numpy.amin(b)
        b_max = numpy.amax(b)
        log.debug("Battery Amps: Min %.2f, Avg %.2f, Max %.2f, %d readings", b_min, b_avg, b_max, b.size)

    return b_avg

def get_ac_rms_current():
    """Get the current measurement from the AC current transducer"""

    amps_adc = 5
    amps_zero = 0
    amps_scale = 0.04369  # Determined empirically

    mcp = Adafruit_MCP3008.MCP3008(spi=adc_spidev)

    amps_rms = list()
    for _ in range(100):
        amps_rms.append(mcp.read_adc(amps_adc))

    # The current transducer already computes the amps as RMS
    a = (amps_zero + numpy.array(amps_rms)) * amps_scale
    a_avg = numpy.mean(a)

    if log.isEnabledFor(logging.DEBUG):
        a_min = numpy.amin(a)
        a_max = numpy.amax(a)
        log.debug("AC Amps RMS: Min %.2f, Avg %.2f, Max %.2f, %d readings", a_min, a_avg, a_max, a.size)

    return a_avg


def upload_metrics(metrics):
    """Send a collection of metrics to the upload API"""

    metrics_collection = list()

    # Generally we're only uploading a single collection of metrics, but leave the option for multiple
    if type(metrics) is list:
        metrics_collection.extend(metrics)
    else:
        metrics_collection.append(metrics)

    # Convert the timestamps to SQL datetime format
    for m in metrics_collection:
        m['timestamp'] = m['timestamp'].strftime('%Y-%m-%d %H:%M:%S')

    json_data = json.dumps({'data': metrics_collection})
    log.debug(json_data)

    try:
        response = requests.post(api_endpoint, auth=api_auth, data=json_data, timeout=30)
        log.info("Upload response: %s", response.text)
        response.raise_for_status()
        if 'error' in response.json():
            raise Exception(response.json()['error'])
    except Exception as e:
        # If the upload failed, save the data for retrying later
        with open(failed_upload_file, 'a') as f:
            f.write(json_data)
            f.write('\n')
        log.error("Failed to upload entry (%s)", str(e))


if __name__ == "__main__":
    status_loop()

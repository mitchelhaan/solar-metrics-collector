#!/usr/bin/env python2

import collections
import datetime
import json
import logging
import numpy
import requests
import time

# ADC support
import Adafruit_ADS1x15

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
                aggregated_stats[key] = numpy.mean(numpy.array(self._metrics[key])).round(2)
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
            # Skip the initial upload, wait until we're on schedule
            if next_upload_time > 0:
                log.info("Uploading aggregated metrics")
                upload_metrics(metrics.aggregate())
            metrics.clear()

            delay = upload_interval_sec - time.time() % upload_interval_sec
            next_upload_time = time.time() + delay

        delay = collection_interval_sec - time.time() % collection_interval_sec
        log.debug("Next collection scheduled in %f seconds", delay)
        log.debug("Next upload scheduled in %f seconds", next_upload_time - time.time())
        time.sleep(delay)


def read_adc(channel, duration):
    """Read values from ADC channel"""
    adc_gain = 1  # Valid gains: 2/3, 1, 2, 4, 8, 16
    adc_rate = 860  # Valid rates: 8, 16, 32, 64, 128, 250, 475, 860 samples per second

    measurements = list()

    # Read in continuous mode
    adc.start_adc(channel, adc_gain, adc_rate)
    start_time = time.time()

    while time.time() - start_time < duration:
        try:
            measurements.append(adc.get_last_result())
        except IOError:
            continue
        time.sleep(1.0/adc_rate)

    adc.stop_adc()

    return numpy.interp(numpy.array(measurements), [0.0, 32767.0], [0.0, 4.096 / adc_gain])


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

    current_metrics['battery_temp'] = float(solar_client.read_input("Battery Temp."))

    current_metrics['battery_volts'] = float(solar_client.read_input("Charging equipment output voltage"))
    current_metrics['battery_amps'] = get_battery_current()
    current_metrics['battery_watts'] = current_metrics['battery_volts'] * current_metrics['battery_amps']

    current_metrics['dc_load_watts'] = current_metrics['battery_volts'] * get_dc_load_current()

    current_metrics['ac_load_watts'] = get_ac_load_power()

    current_metrics['load_watts'] = current_metrics['ac_load_watts'] + current_metrics['dc_load_watts']

    current_metrics['timestamp'] = datetime.datetime.now()

    log.info('Solar: %.2fW Battery: %.2fW DC Load: %.2fW AC Load: %.2fW',
             current_metrics['pv_watts'],
             current_metrics['battery_watts'],
             current_metrics['dc_load_watts'],
             current_metrics['ac_load_watts'])

    return current_metrics


def get_battery_current():
    """Get the current from the battery bank current transducer"""

    adc_channel = 0
    adc_zero = 2.56  # Full-scale of the transducer (5.12 V), divided in half
    adc_scale = 1.0/0.043  # Amps/Volt

    # Calculate the average amps, because the current is 120Hz pulsed from DC->AC conversion
    # Measurement duration must be an even multiple of 120Hz
    measurements = read_adc(adc_channel, 24.0/120.0)

    a = (adc_zero - numpy.array(measurements)) * adc_scale
    a_avg = numpy.mean(a)

    if log.isEnabledFor(logging.DEBUG):
        a_min = numpy.amin(a)
        a_max = numpy.amax(a)
        log.debug("Battery Amps: Min %.2f, Avg %.2f, Max %.2f, %d readings", a_min, a_avg, a_max, a.size)

    return a_avg


def get_dc_load_current():
    """Get the current measurement from the DC current transducer"""

    adc_channel = 1
    adc_zero = 2.5505  # Full-scale of the transducer (5.12 V), divided in half
    adc_scale = -1.0/0.040  # Amps/Volt, inverted to make load positive

    # DC load is fairly consistent, just get a short average
    measurements = read_adc(adc_channel, 0.1)

    a = (adc_zero - numpy.array(measurements)) * adc_scale
    a_avg = numpy.mean(a)

    if log.isEnabledFor(logging.DEBUG):
        a_min = numpy.amin(a)
        a_max = numpy.amax(a)
        log.debug("DC Amps: Min %.2f, Avg %.2f, Max %.2f, %d readings", a_min, a_avg, a_max, a.size)

    return a_avg


def get_ac_load_power():
    """Get the power measurement from the AC power transducer"""

    adc_channel = 2
    adc_scale = 600  # Watts/Volt
    power_offset = 30  # The inverter itself uses a bit of power

    measurements = read_adc(adc_channel, 0.2)

    a = power_offset + numpy.array(measurements) * adc_scale
    a_avg = numpy.mean(a)

    if log.isEnabledFor(logging.DEBUG):
        a_min = numpy.amin(a)
        a_max = numpy.amax(a)
        log.debug("AC Power: Min %.2f, Avg %.2f, Max %.2f, %d readings", a_min, a_avg, a_max, a.size)

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

# Global variables
is_daytime = False
solar_client = EPsolarTracerClient(serialclient=ModbusClient(method='rtu', port='/dev/ttyUSB0', baudrate=115200))
adc = Adafruit_ADS1x15.ADS1115()

logging.basicConfig()
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

if __name__ == "__main__":
    status_loop()

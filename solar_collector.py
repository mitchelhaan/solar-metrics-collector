#!/usr/bin/env python2

import collections
import datetime
import json
import logging
import numpy
import Queue
import requests
import time
import threading

# ADC support
import Adafruit_ADS1x15

# EPSolar Tracer support
from pyepsolartracer.client import EPsolarTracerClient
from pymodbus.client.sync import ModbusSerialClient as ModbusClient


# Constants
api_endpoint = 'https://example.com/api/solar/upload'
api_auth = ('username', 'password')
failed_upload_file = "/opt/solar_upload_failed.json"
battery_state_file = "/var/run/battery.state"

# Collect every 5 seconds, aggregate and upload once per minute during the day, once per 10 mins at night
collection_interval_sec = 5.0
day_upload_interval_sec = 1.0 * 60
night_upload_interval_sec = 10.0 * 60


class StateManager:
    def __init__(self, state_file, defaults=None):
        self._defaults = defaults if defaults is not None else dict()
        try:
            self._state_fp = open(state_file, mode='r+', buffering=0)
        except IOError:
            self._state_fp = open(state_file, mode='w+', buffering=0)

    def __enter__(self):
        try:
            self._state_fp.seek(0)
            self._state_dict = self._defaults.copy()
            self._state_dict.update(json.load(self._state_fp))
        except ValueError:
            pass

        return self._state_dict

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._state_fp.seek(0)
        self._state_fp.truncate()
        json.dump(self._state_dict, self._state_fp)
        self._state_fp.close()


class SealedLeadAcidBatterySoC:
    _cell_resting_voltage = 2.1
    _cell_float_voltage = 2.3
    _cell_absorption_voltage = 2.4
    _cell_equalization_voltage = 2.433333

    _defaults = dict()
    _defaults['remaining_capacity_ah'] = 0.0
    _defaults['charging_correction_factor'] = 1.0
    _defaults['discharging_correction_factor'] = 1.0

    def __init__(self, capacity_ah, cell_count=6):
        self.average_current = 0.0
        self.total_capacity_ah = capacity_ah
        self.cell_count = cell_count

    def set_percent_charged(self, percent_charged):
        self.set_remaining_capacity(self.total_capacity_ah * (percent_charged / 100.0))

    def get_percent_charged(self):
        with StateManager(battery_state_file, self._defaults) as b_state:
            charged = b_state['remaining_capacity_ah'] / self.total_capacity_ah * 100.0
            return clamp_value(charged, 0.0, 100.0)

    def set_remaining_capacity(self, capacity):
        with StateManager(battery_state_file, self._defaults) as b_state:
            old_ah = b_state['remaining_capacity_ah']
            new_ah = clamp_value(capacity, 0.0, self.total_capacity_ah)
            b_state['remaining_capacity_ah'] = new_ah
            log.info("Updated battery charge state: Was %.2f Ah, now %.2f Ah", old_ah, new_ah)

    def get_remaining_capacity(self):
        with StateManager(battery_state_file, self._defaults) as b_state:
            return clamp_value(b_state['remaining_capacity_ah'], 0.0, self.total_capacity_ah)

    def estimate_capacity_from_voltage(self, voltage, current=0.0, float_charging=False, temperature=20.0):
        """Estimating capacity under load/charging is quite difficult, so for now only do it while floating"""
        c_rate = current / self.total_capacity_ah
        cell_voltage = voltage / float(self.cell_count)

        # Floating, charge rate is < 0.01C
        if float_charging and 0.0 <= c_rate <= 0.01 and abs(cell_voltage - self._cell_float_voltage) <= 0.1:
            return self.total_capacity_ah

        # Floating, charge rate is 0.01C - 0.1C
        if float_charging and 0.01 < c_rate <= 0.1 and abs(cell_voltage - self._cell_float_voltage) <= 0.1:
            percent_est = 1.0 + 0.2 * (0.01 - c_rate)
            return self.total_capacity_ah * percent_est

        return 0.0

    def update(self, amp_hours, temperature=20.0):
        with StateManager(battery_state_file, self._defaults) as b_state:
            if amp_hours > 0:
                amp_hours *= b_state['charging_correction_factor']
            else:
                amp_hours *= b_state['discharging_correction_factor']

            new_remaining_capacity = b_state['remaining_capacity_ah'] + amp_hours
            b_state['remaining_capacity_ah'] = clamp_value(new_remaining_capacity, 0.0, self.total_capacity_ah)

            log.debug("Battery SoC: %.3f (%.3f) Ah, %s", new_remaining_capacity, amp_hours, json.dumps(b_state))


class MetricsCollection:
    """Simple class to collect and average metrics"""

    # Special cases for certain metrics
    _most_recent = ['timestamp', 'kwh_today', 'kwh_total', 'battery_charge']
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


class MetricUploader:
    def __init__(self):
        self._queue = Queue.Queue()
        self._thread = threading.Thread(target=self._run, args=())
        self._thread.daemon = True
        self._thread.start()

    def _run(self):
        while True:
            metric_collection = self._queue.get(block=True)

            # Reformat the timestamp value for MySQL
            metric_collection['timestamp'] = metric_collection['timestamp'].strftime('%Y-%m-%d %H:%M:%S')

            json_data = json.dumps({'data': [metric_collection]})
            log.debug(json_data)

            try:
                response = requests.post(api_endpoint, auth=api_auth, data=json_data, timeout=30)
                log.info("Upload response for %s: %s", metric_collection['timestamp'], response.text)
                response.raise_for_status()
                if 'error' in response.json():
                    raise Exception(response.json()['error'])
            except Exception as e:
                # If the upload failed, save the data for retrying later
                with open(failed_upload_file, 'a') as f:
                    f.write(json_data)
                    f.write('\n')
                log.error("Failed to upload entry (%s)", str(e))

            self._queue.task_done()

    def enqueue(self, metric_collection):
        if type(metric_collection) is list:
            for m in metric_collection:
                self._queue.put(m)
        else:
            self._queue.put(metric_collection)


def status_loop():
    """Main loop, runs continuously gathering metrics"""

    solar_client.connect()

    metrics = MetricsCollection()
    metric_uploader = MetricUploader()

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
                metric_uploader.enqueue(metrics.aggregate())
            metrics.clear()

            delay = upload_interval_sec - time.time() % upload_interval_sec
            next_upload_time = time.time() + delay

        delay = collection_interval_sec - time.time() % collection_interval_sec
        log.debug("Next collection scheduled in %f seconds", delay)
        log.debug("Next upload scheduled in %f seconds", next_upload_time - time.time())
        time.sleep(delay)


def clamp_value(val, min_val, max_val):
    """Restrict a value to within the specified range"""
    return min_val if val < min_val else max_val if val > max_val else val


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

    # Only force the charge state if we've gotten significantly off track
    if current_metrics['pv_charging_mode'] == 'Float' and battery_monitor.get_percent_charged() < 98.0:
        est = battery_monitor.estimate_capacity_from_voltage(current_metrics['battery_volts'],
                                                             current_metrics['battery_amps'], float_charging=True)
        battery_monitor.set_remaining_capacity(est)

    ah_this_interval = current_metrics['battery_amps'] * (collection_interval_sec / 3600.0)
    battery_monitor.update(amp_hours=ah_this_interval)
    current_metrics['battery_charge'] = round(battery_monitor.get_percent_charged(), 2)

    current_metrics['dc_load_watts'] = current_metrics['battery_volts'] * get_dc_load_current()

    current_metrics['ac_load_watts'] = get_ac_load_power()

    current_metrics['load_watts'] = current_metrics['ac_load_watts'] + current_metrics['dc_load_watts']

    current_metrics['timestamp'] = datetime.datetime.now()

    log.info('Estimated battery SoC: %.3f Ah (%.3f%%) %.2f V',
             battery_monitor.get_remaining_capacity(),
             battery_monitor.get_percent_charged(),
             current_metrics['battery_volts'])

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


# Global variables
is_daytime = False
solar_client = EPsolarTracerClient(serialclient=ModbusClient(method='rtu', port='/dev/ttyUSB0', baudrate=115200))
battery_monitor = SealedLeadAcidBatterySoC(capacity_ah=125.0, cell_count=24)
adc = Adafruit_ADS1x15.ADS1115()

logging.basicConfig()
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

if __name__ == "__main__":
    status_loop()

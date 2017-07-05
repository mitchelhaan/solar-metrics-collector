<?php

const MYSQL_SERVER = 'localhost';
const MYSQL_USER = 'username';
const MYSQL_PASS = 'password';
const MYSQL_DB = 'solar';

const STATS_FIELDS = 'timestamp, pv_volts, pv_amps, pv_watts, load_watts, kwh_today, kwh_total, pv_charging_mode, battery_volts, battery_amps, battery_watts, battery_charge, battery_temp';

const AUTH_USER = 'solar';

const UPLOAD_TOKENS = array(
	'<upload token>' => array('home')
);

const STATS_TOKENS = array(
	'<stats token>' => array('home')
);

$api_path = explode('/', strtolower($_REQUEST['api']));
if (count($api_path) < 2)
{
	return_error('400 Bad Request', 'You must include at least an action and location (/api/solar/action/location)');
}

$action = $api_path[0];
$location = $api_path[1];

assert_valid_location_token($action, $location);

switch ($action)
{
	case 'upload':
		assert_request_method('POST');

		$data = json_decode(file_get_contents('php://input'));
		$result = insert_solar_log_entries($location, $data);
		$result['processingTime'] = get_elapsed_time();

		header('Content-Type: application/json');
		print(json_encode($result));
		break;

	case 'stats':
		assert_request_method('GET');

		$data = get_solar_log_entries($location);

		header('Content-Type: application/json');
		print(json_encode(array('data' => $data, 'processingTime' => get_elapsed_time())));
		break;

	case 'current-stats':
		assert_request_method('GET');

		$params = array('limit' => 1);
		$data = get_solar_log_entries($location, $params);

		header('Access-Control-Allow-Origin: *');
		header('Content-Type: application/json');
		print(json_encode(array('data' => $data, 'processingTime' => get_elapsed_time())));
		break;

	case 'daily-stats':
		assert_request_method('GET');

		$data = get_daily_solar_stats($location);

		header('Access-Control-Allow-Origin: *');
		header('Content-Type: application/json');
		$data['processingTime'] = get_elapsed_time();
		print(json_encode($data));
		break;

	default:
		return_error('404 Not Found');
		break;
}

exit();

/****************************************************************************************/

function insert_solar_log_entries($location, $request)
{
	// Create connection
	$conn = new mysqli(MYSQL_SERVER, MYSQL_USER, MYSQL_PASS, MYSQL_DB);

	// Check connection
	if ($conn->connect_error) {
		return_error('503 Service Unavailable', 'Failed to connect to database');
	}

	$stmt = $conn->prepare("INSERT INTO stats_log_{$location} (timestamp, pv_volts, pv_amps, pv_watts, kwh_today, kwh_total, pv_charging_mode, battery_volts, battery_amps, battery_watts, load_watts, battery_charge, battery_temp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)");
	if ($stmt === FALSE)
	{
		return_error('503 Service Unavailable', 'Failed to connect to database');
	}
	$stmt->bind_param("sdddddsdddddd", $timestamp, $pv_volts, $pv_amps, $pv_watts, $kwh_today, $kwh_total, $pv_charging_mode, $battery_volts, $battery_amps, $battery_watts, $load_watts, $battery_charge, $battery_temp);

	$inserted_count = 0;

	foreach ($request->data as $log_entry)
	{
		$timestamp = $log_entry->timestamp;
		$pv_volts = $log_entry->pv_volts;
		$pv_amps = $log_entry->pv_amps;
		$pv_watts = $log_entry->pv_watts;
		$kwh_today = $log_entry->kwh_today;
		$kwh_total = $log_entry->kwh_total;
		$pv_charging_mode = $log_entry->pv_charging_mode;
		$battery_volts = $log_entry->battery_volts;
		$battery_amps = $log_entry->battery_amps;
		$load_watts = $log_entry->load_watts;
		$battery_watts = $log_entry->battery_watts;
		$battery_charge = $log_entry->battery_charge;
		$battery_temp = $log_entry->battery_temp;

		if ($stmt->execute())
		{
			$inserted_count++;
		}
	}

	$total_count = count($request->data);

	if ($conn->errno || $inserted_count != $total_count)
	{
		$result = array('error' => "Processing failed (error: $conn->error ($conn->errno)), $inserted_count / $total_count entries processed");
	}
	else
	{
		$result = array('message' => "Successfully processed $inserted_count entries");
	}

	$stmt->close();
	$conn->close();

	return $result;
}


function get_solar_log_entries($location, $args=array())
{
	$args['start_timestamp'] = !isset($args['start_timestamp']) ? '1000-01-01 00:00:00' : $args['start_timestamp'];
	$args['end_timestamp'] = !isset($args['end_timestamp']) ? '9999-12-31 23:59:59' : $args['end_timestamp'];
	$args['offset'] = !isset($args['offset']) ? 0 : $args['offset'];
	$args['limit'] = !isset($args['limit']) ? 1000 : $args['limit'];

	// Create connection
	$conn = new mysqli(MYSQL_SERVER, MYSQL_USER, MYSQL_PASS, MYSQL_DB);

	// Check connection
	if ($conn->connect_error) {
		return_error('503 Service Unavailable', 'Failed to connect to database');
	}

	$stmt = $conn->prepare("SELECT ".STATS_FIELDS." FROM stats_log_{$location} WHERE `timestamp` BETWEEN ? AND ? ORDER BY `timestamp` DESC LIMIT ? OFFSET ?");
	if ($stmt === FALSE)
	{
		return_error('503 Service Unavailable', 'Failed to connect to database');
	}

	$stmt->bind_param("ssii", $args['start_timestamp'], $args['end_timestamp'], $args['limit'], $args['offset']);

	$stmt->execute();

	// Get metadata for field names
	$meta = $stmt->result_metadata();

	// Create an array of variables to use to bind the results
	$fields = array();
	while ($field = $meta->fetch_field()) {
		$var = $field->name;
		$$var = null;
		$fields[$var] = &$$var;
	}
	call_user_func_array(array($stmt, 'bind_result'), $fields);

	$results = array();
	while ($stmt->fetch())
	{
		$results[] = array_deep_clone($fields);
	}

	// The double reversal is so we get the x most recent entries, but still in ascending order
	return array_reverse($results);
}


function get_daily_solar_stats($location)
{
	$args['start_date'] = date('Y-m-d', strtotime('-4 weeks'));
	$args['end_date'] = date('Y-m-d');
	return get_solar_stats_entries($location, $args);
}


function get_solar_stats_entries($location, $args=array())
{
	$args['start_date'] = !isset($args['start_date']) ? '1000-01-01' : $args['start_date'];
	$args['end_date'] = !isset($args['end_date']) ? '9999-12-31' : $args['end_date'];
	$args['offset'] = !isset($args['offset']) ? 0 : $args['offset'];
	$args['limit'] = !isset($args['limit']) ? 1000 : $args['limit'];

	// Initialize the result data
	$result = array(
		'cols' => array(
			array('id' => 'date', 'label' => 'Date', 'type' => 'date'),
			array('id' => 'max_pv_watts', 'label' => 'Max Solar Watts', 'type' => 'number'),
			array('id' => 'kwh', 'label' => 'Energy Generated', 'type' => 'number'),
			array('id' => 'avg_kwh', 'label' => 'Average', 'type' => 'number'),
			array('role' => 'tooltip', 'type' => 'string'), // 'p' => array('html' => 'true')
			array('role' => 'style', 'type' => 'string'),
		),
		'rows' => array()
	);

	// Create connection
	$conn = new mysqli(MYSQL_SERVER, MYSQL_USER, MYSQL_PASS, MYSQL_DB);

	// Check connection
	if ($conn->connect_error) {
		return_error('503 Service Unavailable', 'Failed to connect to database');
	}

	$stmt = $conn->prepare("SELECT ROUND(AVG(`kwh`), 2) AS `avg_kwh` FROM stats_daily_{$location} WHERE `date` BETWEEN ? AND ? ORDER BY `date` LIMIT ? OFFSET ?");
	if ($stmt === FALSE)
	{
		return_error('503 Service Unavailable', 'Failed to connect to database');
	}

	$stmt->bind_param("ssii", $args['start_date'], $args['end_date'], $args['limit'], $args['offset']);
	$stmt->execute();
	$stmt->bind_result($r_avg_kwh);
	$stmt->fetch();
	$stmt->close();

	$stmt = $conn->prepare("SELECT `date`, `max_pv_watts`, `kwh`, `fully_charged` FROM stats_daily_{$location} WHERE `date` BETWEEN ? AND ? ORDER BY `date` LIMIT ? OFFSET ?");
	if ($stmt === FALSE)
	{
		return_error('503 Service Unavailable', 'Failed to connect to database');
	}

	$stmt->bind_param("ssii", $args['start_date'], $args['end_date'], $args['limit'], $args['offset']);
	$stmt->execute();
	$stmt->bind_result($r_date, $r_max_pv, $r_kwh, $r_fully_charged);
	while ($stmt->fetch())
	{
		list($y, $m, $d) = explode('-', $r_date);
		$color = ($r_fully_charged) ? '#30b82e' : '#f14141';
		$charged_text = ($r_fully_charged) ? '' : 'not';

		$result['rows'][] = array('c' => array(
			array('v' => sprintf('Date(%d, %d, %d)', $y, $m-1, $d)),
			array('v' => $r_max_pv),
			array('v' => $r_kwh),
			array('v' => $r_avg_kwh),
//			array('v' => "<b>Date:</b> $r_date<br><b>Generated:</b> $r_kwh kWh<br>Batteries were $charged_text fully charged"),
			array('v' => "Date: $r_date\nGenerated: $r_kwh kWh\nBatteries were $charged_text fully charged"),
			array('v' => "color: $color"),
		));
	}
	$stmt->close();

	return $result;
}


function assert_valid_location_token($action, $location)
{
	$user = $_SERVER['PHP_AUTH_USER'];
	$token = $_SERVER['PHP_AUTH_PW'];

	// Make sure location doesn't have any invalid characters
	if (preg_match('/[^a-zA-Z0-9_]/', $location) !== 0)
	{
		return_error('400 Bad Request', 'Invalid location parameter');
	}

	switch ($action)
	{
		case 'upload':
			if ($user !== AUTH_USER)
			{
				return_error('401 Unauthorized');
			}
			if (!in_array($location, UPLOAD_TOKENS[$token]))
			{
				return_error('403 Forbidden');
			}
			break;

		case 'stats':
			if ($user !== AUTH_USER)
			{
				return_error('401 Unauthorized');
			}
			if (!in_array($location, STATS_TOKENS[$token]))
			{
				return_error('403 Forbidden');
			}
			break;

		case 'current-stats':
		default:
			// No token required, assuming the action is valid
			break;

		case 'daily-stats':
		default:
			// No token required, assuming the action is valid
			break;
	}
}


function assert_request_method($allowed)
{
	if ((is_array($allowed) && !in_array($_SERVER['REQUEST_METHOD'], $allowed)) ||
		(is_string($allowed) && $_SERVER['REQUEST_METHOD'] != $allowed))
	{
		return_error('405 Method Not Allowed');
	}
}


function return_error($status_code='500 Internal Server Error', $error_msg='')
{
	// If no explicit error message was provided, scrape a default one from the status code
	if ($error_msg == '')
	{
		$error_msg = preg_replace('/\d+ (.*)/', '$1', $status_code);
	}

	// If we need authentication, make sure to add the authenticate header
	if (substr($status_code, 0, 3) == '401')
	{
		header('WWW-Authenticate: Basic realm="Solar API"');
	}

	// Add the header for the error we're returning
	header($_SERVER['SERVER_PROTOCOL'] . ' ' . $status_code);

	// Also give the error message in the body if more than the headers were requested
	if ($_SERVER['REQUEST_METHOD'] != 'HEAD')
	{
		header('Content-Type: application/json');
		die(json_encode(array('error' => $error_msg)));
	}
	else
	{
		die();
	}
}


function array_deep_clone(array $array)
{
	$result = array();
	foreach ($array as $key => $val)
	{
		if (is_array($val))
		{
			$result[$key] = array_deep_clone($val);
		}
		elseif (is_object($val))
		{
			$result[$key] = clone $val;
		} else {
			$result[$key] = $val;
		}
	}
	return $result;
}


function get_elapsed_time()
{
	return microtime(true) - $_SERVER["REQUEST_TIME_FLOAT"];
}

?>

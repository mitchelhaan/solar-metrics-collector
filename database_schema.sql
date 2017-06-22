CREATE DATABASE IF NOT EXISTS `solar` DEFAULT CHARACTER SET utf8 COLLATE utf8_general_ci;
USE `solar`;

CREATE TABLE `stats_log_home` (
  `timestamp` datetime NOT NULL,
  `pv_volts` float NOT NULL,
  `pv_amps` float NOT NULL,
  `pv_watts` float NOT NULL,
  `kwh_today` float NOT NULL,
  `kwh_total` float NOT NULL,
  `pv_charging_mode` enum('Not charging','Float','MPPT','Equalization') NOT NULL DEFAULT 'Not charging',
  `battery_volts` float NOT NULL,
  `battery_amps` float NOT NULL,
  `battery_watts` float NOT NULL,
  `battery_temp` float NOT NULL,
  `load_watts` float NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8;

ALTER TABLE `stats_log_home`
  ADD PRIMARY KEY (`timestamp`);


CREATE TABLE `stats_daily_home` (
  `date` date NOT NULL,
  `day_length` time NOT NULL,
  `max_pv_watts` float NOT NULL,
  `kwh` float NOT NULL,
  `fully_charged` tinyint(1) NOT NULL DEFAULT '0',
  `calculated` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8;

ALTER TABLE `stats_daily_home`
  ADD PRIMARY KEY (`date`);


CREATE TABLE `stats_monthly_home` (
  `date` date NOT NULL,
  `avg_day_length` time NOT NULL,
  `kwh` float NOT NULL,
  `calculated` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8;

ALTER TABLE `stats_monthly_home`
  ADD PRIMARY KEY (`date`);


-- Some simple triggers and procedures to do automated stats rollup

DELIMITER $$
CREATE PROCEDURE `compute_daily_stats_home` (IN `pDate` DATE)  MODIFIES SQL DATA
    DETERMINISTIC
BEGIN
REPLACE INTO `stats_daily_home` SELECT DATE(`timestamp`) AS `date`, TIMEDIFF(MAX(`timestamp`), MIN(`timestamp`)) AS `day_length`, MAX(`pv_watts`) AS `max_pv_watts`, MAX(`kwh_today`) AS `kwh`, SUM(IF(`pv_charging_mode` = 'Float', 1, 0)) > 0 AS `fully_charged`, NOW() AS `calculated` FROM (SELECT `timestamp`,`kwh_today`,`pv_watts`,`pv_charging_mode` FROM `stats_log_home` WHERE `timestamp` >= pDate AND `timestamp` < ADDDATE(pDate, INTERVAL 1 DAY) AND `pv_watts` > 0) AS `day_data`;
END$$

CREATE TRIGGER `compute_daily_stats` AFTER INSERT ON `stats_log_home` FOR EACH ROW
BEGIN
  IF NEW.pv_watts = 0 AND TIME(NEW.timestamp) > '12:00' AND (SELECT COUNT(*) FROM stats_daily_home WHERE date = DATE(NEW.timestamp)) = 0 THEN
    CALL compute_daily_stats_home(DATE(NEW.timestamp));
  END IF;
END
$$

CREATE PROCEDURE `compute_monthly_stats_home` (IN `pDate` DATE)  MODIFIES SQL DATA
    DETERMINISTIC
BEGIN
SET @first_day = DATE_ADD(pDate, INTERVAL -DAY(pDate) + 1 DAY);
SET @last_day = LAST_DAY(pDate);
REPLACE INTO `stats_monthly_home`
  SELECT @last_day AS `date`,
    SEC_TO_TIME(AVG(TIME_TO_SEC(`day_length`))) AS `avg_day_length`,
    SUM(`kwh`) AS `kwh`,
    NOW() AS `calculated`
  FROM (SELECT `day_length`,`kwh` FROM `stats_daily_home` WHERE `date` BETWEEN @first_day AND @last_day) AS `month_data`;
END$$

CREATE TRIGGER `compute_monthly_stats` AFTER INSERT ON `stats_daily_home` FOR EACH ROW
BEGIN
  IF NEW.date = LAST_DAY(NEW.date) THEN
    CALL compute_monthly_stats_home(NEW.date);
  END IF;
END
$$
DELIMITER ;

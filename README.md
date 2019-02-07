#### NOTE: Please review our code of conduct and project README files prior to accessing these project files.

# BF-001-IFM-PYTHON
Daemon for IFM temperature and vibration sensors

# Purpose
The Corlina sensor monitoring daemon is a service level application running on an integration server. Its job functions are, in frequency order:

- read the sensor data values and normalize them
- write the sensor data to the InfluxDB database
- check the sensor values and if they exceed thresholds, report them to the Corlina SaaS
- read the sensor types and port allocation on the IO-Link master interface device
- read the IO-Link master’s internal sensor values
- write the IO-Link sensor results to the InfluxDB database
- collect the configuration of the IO-Link master to check state and save it
- compare the state of the IO-Link master to saved state and report changes; if the changes exceed thresholds, report them to the Corlina SaaS.

# Settings
The settings for the operation of the sensor logging daemon come from the following sources:
- Application properties file sensorcap.properties
- The environment variables, specifically RUNTIME_DEBUG_LEVEL, which sets the messaging level for the application’s log file

# Sensorcap.properties
The following sections are a description of the properties file used to configure the sensor monitoring daemon.

## General Settings
The general settings are related to the operation of the daemon’s background processing and are not tied to the specifics of the sensors. 

| Value         | Purpose       | Default |
| ------------- | ------------- |---------|
| logfile       | Specifies the name and location (directory) the daemon’s log data is written to  | ./sensormgr.log |
| devicelist | Enumerates a comma-delimited list of IO-Link master devices (the interface blocks which connect to sensors) to be processed by the daemon (a sub-process is spawned per device) | |
| httptimeout | Time out value (specified in floating point form) to be used for http get requests to the IO-Link masters | 1.0 seconds |
| loglevel | Level of message to write to the application log file (possible values are CRITICAL, ERROR, WARN, DEBUG, INFO, and NOTSET) | WARN |

## IO-Link Master Settings
The IO-Link master settings are those that specify the connection information for each IO-Link interface device, specifically the IP address, IP port, and timeout delay allowed. 

| Value         | Purpose       | Default |
| ------------- | ------------- |---------|
| \<IOLM>\.ipaddress | IP address of the IO-Link master device; used to invoke http interface to retrieve data  | |
| \<IOLM>\.port | IP port number for connection to the IO-Link interface device | 80 |
| \<IOLM>\.delay | Number pair in the form of loop timing in seconds, and number of loop iterations to complete before refreshing IO-Link device information. The default of 10,6 would mean that the sensor read loop is 10 seconds and every 6th iteration the configuration details of the system are retrieved to check against existing values. The loop timing range is limited to a minimum of 5 seconds and a maximum of 200 seconds to ensure the IOLM devices are not queried too often nor too infrequently. The iteration value is limited to a minimum of 5 and maximum of 20 to avoid being too chatty with the IOLM and missing configuration changes respectively| 10,6 |

### NOTE 
\<IOLM>\ is the unique string defined in the device list for which the above parameters set the connection details. If the devicelist entry was myIOLM then the address would be myIOLM.ipaddress. the port would be myIOLM.port, and the delay would be myIOLM.delay. This allows for per interface device (IO-Link master) setting to be defined individually.

## InfluxDB Connection Details
There are three elements loaded from the properties file to connect the daemon to the InfluxDB database instance used to hold the readings and tags for presentation in graphical format to the user.

| Value         | Purpose       | Default |
| ------------- | ------------- |---------|
| influxhost | is the IP address for connection to database | localhost |
| influxport | is the IP port for connection to database | 	8086 |
| Influxdatabase | is the	“Schema” for data to be written | sensor |

## Chronograf
Chronograf is used as the dashboard tool for rendering the information capture on a periodic basis. It is used to display a time-series of data managed by Influx.

Chronograf specific documentation on configuration can be found here: https://docs.influxdata.com/chronograf/v1.7/

The installation of the Chronograf software on the Linux server was completed with the out of the box standard settings:

- Service Name:	Chronograf
- IP Port:	8888
- Source Database:	Influxdb (sensor)

## Device Specific Settings
The device specific settings are used to tie the sensor values read to the reporting thresholds that control whether an “event” is reported to the Corlina SaaS. The format of the settings is derived from the IO-Link sensor standards. These standards uniquely identify a sensor’s manufacturer and its uniquely assigned serial number, combined with the reporting threshold name and its trigger value. The @symbol is used as a delimiter for the parts of the identifier.

Syntax:	  \<vendor-id>\@\<sensor-serial-number>\@\<threshold-name>\=\<threshold-value>\
Samples:  310@2729@acceleration=1.0
          310@0003848155@temperaturemax=100.0

### NOTE 
ifm Gmbh’s vendor ID is 310; the complete list of vendor numbers is available at the IO-Link organization’s website here: http://www.io-link.com/share/Downloads/Vendor_ID_Table.xml

For ifm TA-type temperature sensors, there are two limit thresholds (temperaturemin and temperaturemax) which are the temperature in degrees Celsius with one decimal place.

For ifm JN-type vibration sensors there are two limit properties (acceleration and velocity); acceleration is measured in milligravities and velocity is measured in millimeters per second with both properties supporting 2 decimal places.

## Application Execution
When executing, the application creates an independent sub-process to handle the communication with each IO-Link master. The details for the connection to each IOLM are defined in the devicelist, which lists the IOLMs and creates a definition template for the application to look up the connection specifics.

## Application Operation 
The monitoring daemon (daemon3.py) is implemented as an Ubuntu system service, which is set up to start by the system service daemon “systemd”; configuration of the operation of the service are managed through a combination of a configuration file daemon.service and the systemctl configuration tool.

## Daemon3.service File
[Unit] Description=Monitoring HTTP service 
After=network.target influxd.service 
StartLimitIntervalSec=0 
[Service] 
Type=simple 
Restart=always 
RestartSec=1 
User=localhost 
ExecStart=/home/localhost/PycharmProjects/sensormgr/venv/bin/python3 /home/localhost/PycharmProjects/sensormgr/venv/daemon3.py

[Install] 
WantedBy=multi-user.target

This file sets the startup and running options, including restarting and references the execution location of the source Python file and the correctly configured interpreter environment to run the code successfully.

The service file is defined and then linked to the systemd susbsystem by linking it using: systemctl link daemon3.py from its source directory. The file must be owned by root and belong to the root group which can be achieve by using chmod and chgrp respectively.

The daemon code will run unattended and will be started and stopped by the operating system automatically; however the service’s execution can be manually modified by issuing systemctl commands in the following formats:

	Start:	systemctl start daemon3 
	Stop:	systemctl stop daemon3 
	Enable Auto-Start:	systemctl enable daemon3 
	Disable Auto-Start:	systemctl disable daemon3 

# Log Management

## Log Files 
The log file for the daemon3 monitor is “sensormgr.log” Logs are written to the directory “/var/log/daemon3” by default and the Linux utility logrotate is engaged to ensure log size and disk storage remain manageable. Logs will age out and be deleted over time and the current log size will be kept to a size that allows browsing.

The log location is managed in the sensorcap.properties file in the daemon’s home directory.

Log rotation management is handled in the log rotation daemon’s (logrotate) configuration file directory /etc/logrotate.d; the daemon3 service specific configuration file is /etc/logrotate.d/daemon3 with the logs for the last 7 days being retained. Log rotation is necessary to prevent the system from becoming overloaded with log files and running out of file storage.

/etc/logrotate.d/daemon3 File 
/var/log/daemon3/*.log { 

daily 

rotate 7 

missingok 

dateext 

copytruncate 

compress 

notifempty 

}

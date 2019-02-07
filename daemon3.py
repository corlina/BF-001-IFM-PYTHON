#!/usr/bin/python3 -u
"""
This module communicates with one or more IoT enabled IO-Link masters.
The masters handle the low-level management of sensor communication and
provide an http get/post based means for managing the sensor configuration,
data, and communications.

Note:
    The application expects a Python 3.5 venv with the required modules loaded
    and a properties file with the configuration details for connectivity with
    the IO-Link master, the InfluxDB database, etc.

Todo:
    * deployment
    * documentation
    * improve the status code 513 handling
"""
import sys
import configparser
import requests
import json
from influxdb import InfluxDBClient
import influxdb
from pathlib import Path
import socket
from IPy import IP
import re
import multiprocessing
import time
import logging
from datetime import datetime
import os
import subprocess


class Properties(object):
    """
    Manage the interface to the properties (settings) file for the application
    """
    def __init__(self):
        self.name = '/home/localhost/sensorcap.properties'

    def load(self):
        """
        load the content of the application properties flie
        :return: dictionary of properties loaded from file
        """
        # check for capture properties
        capturepath = Path(self.name)
        if capturepath.is_file():
            logging.info('Configuration file, %s, found', self.name)
            return self.load_properties()
        else:
            # no configuration found
            logging.error('Configuration file, %s, not found', self.name)
            raise Exception('%s configuration file not found', self.name)

    def load_properties(self, sep='=', comment_char='#'):
        """
        Read the file passed as parameter as a properties file
        :return: list of properties and value in dictionary form
        """
        logging.info('Loading properties from file')
        props = {}
        key = ''
        value = ''
        with open(self.name, "rt") as f:
            for line in f:
                lstrip = line.strip()
                if lstrip and not lstrip.startswith(comment_char):
                    key_value = lstrip.split(sep)
                    key = key_value[0].strip()
                    value = sep.join(key_value[1:]).strip().strip('"')
                props[key] = value
            logging.info('%d properties read', len(props))
        return props


class GlobalSettings:
    """
    Handle the application wide settings
    """

    def __init__(self, dp):
        """
        Instance initializer for GlobalSettings
        :param dp: dictionary of properties
        """
        self.httptimeout = dp.get('httptimeout', 1.0)
        self.influxhost = dp.get('influxhost', 'localhost')
        self.influxport = dp.get('influxport', 8086)
        self.influxdatabase = dp.get('influxdatabase', 'sensor')
        self.logfile = dp.get('logfile', './sensormgr.log')

        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        logging.basicConfig(
            filename=self.logfile,
            level=logging.DEBUG,
            format='%(asctime)s.%(msecs)03d %(levelname)s %(module)s - %(funcName)s: %(message)s',
            datefmt="%Y-%m-%d %H:%M:%S")

        self.devicelist = dp.get('devicelist', '')

        debuglevel = dp.get('debuglevel', 'INFO')
        if debuglevel == 'INFO':
            logging.getLogger().setLevel('INFO')
        elif debuglevel == 'ERROR':
            logging.getLogger().setLevel('ERROR')
        elif debuglevel == 'CRITICAL':
            logging.getLogger().setLevel('CRITICAL')
        elif debuglevel == 'DEBUG':
            logging.getLogger().setLevel('DEBUG')
        elif debuglevel == 'NOTSET':
            logging.getLogger().setLevel('NOTSET')
        else:
            logging.getLogger().setLevel('WARN')

        logging.info('Logging initiated')
        # check for property 'devicelist' in property dictionary
        if len(self.devicelist) < 1:
            logging.error('No devicelist property located during initialization')
            raise Exception("devicelist not found in property strings")


class IOLinkMasterInformation:
    """
    Structures  information about an IO-Link master device
    """
    def __init__(self, prodcode='', ipaddr=''):
        """
        Instance initializer for IOLinkMasterInformation
        :param prodcode: product code
        :param ipaddr: IP address
        """
        self.numports = -1
        self.ipaddress = str(resolveipaddress(ipaddr))      # handle octet and name based addresses
        self.temperature = -273
        self.milliamperes = -1
        self.voltage = -1
        self.supervisionstatus = -1
        self.productcode = prodcode
        self.productserial = ''
        self.family = ''
        self.vendor = ''


class IOLinkMaster(object):
    """
    Implement an abstraction of the IO-Link Master physical device for sensor integration
    """

    def __init__(self, prodcode='', ipaddr='', prt=80, dly=-1):
        """
        Instance initializer for IOLinkMaster
        :param prodcode: product code
        :param ipaddr: IP address
        :param prt: IP port
        :param dly: delay (cycle wait time)
        """
        self.port = prt
        self.delay = dly
        self.duration = 10
        self.iterations = 6
        self.masterinformation = IOLinkMasterInformation(prodcode, ipaddr)
        self.configfile = self.masterinformation.ipaddress
        self.proc = None
        self.devicename = ''
        self.ipaddress = ipaddr
        logging.info('Initializing IOLM, %s', self.masterinformation.productcode)

    def getreadings(self, ipx, gsr, prr):
        """
        Get non-volatile information about the IO-Link integration device
        :param ipx: ip address of IOLM as octet string
        :param gsr: global settings
        :param prr: properties
        :return: None
        """
        # set up influxDB database, and housekeep
        logging.info('(%s) Connecting to InfluxDB', ipx)
        db = Database(ipx, gsr)
        client = db.connect(ipx, gsr)
        if client is None:
            logging.error('(%s) Cannot connect to InfluxDB sensor database', ipx)
            return 'NoConnection'

        savesensorlist = None
        iolminfo = IOLinkMasterInformation()
        sensorlist = []
        fullinfo = True
        # get the number of ports on device
        iolminfo.numports = self.getportcount(ipx)

        itercount = 0
        if iolminfo.numports is not None:
            while True:

                itercount = itercount + 1
                if fullinfo:
                    iolminfo.ipaddress = self.getipaddress(ipx)
                    # get the device descriptive information

                    iolminfo.productserial = self.getproductserial(ipx)
                    iolminfo.vendor = self.getvendor(ipx)
                    iolminfo.family = self.getdevicefamily(ipx)

                    # get the device state information
                    iolminfo.temperature = self.gettemperature(ipx)
                    iolminfo.supervisionstatus = self.getsupervisionstatus(ipx)
                    iolminfo.milliamperes = self.getmilliamperes(ipx)
                    iolminfo.productcode = self.getproductcode(ipx)
                    iolminfo.voltage = self.getvolts(ipx)

                    logging.info('(%s) Writing IOLM data points to Influx', ipx)
                    data = [
                        dict(measurement="iolmEvents",
                             tags={'iolmip': iolminfo.ipaddress, 'serial': iolminfo.productserial,
                                   'vendor': iolminfo.vendor, 'family': iolminfo.family,
                                   'productcode': iolminfo.productcode, 'ports': iolminfo.numports},
                             time=datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                             fields=dict(volts=iolminfo.voltage, milliamperes=iolminfo.milliamperes,
                                         temperature=iolminfo.temperature, status=iolminfo.supervisionstatus))
                    ]

                    try:
                        client.write_points(data)
                    except influxdb.exceptions.InfluxDBClientError as excp:
                        logging.error('(%s) Client error writing IOLM points - Error (%d)',
                                      ipx, excp.errno)
                    except influxdb.exceptions.InfluxDBClientError as excp:
                        logging.error('(%s) Server error writing IOLM points - Error (%d)',
                                      ipx, excp.errno)

                    # add the ports with sensors connected
                    sensorlist = self.getsensorsondevices(ipx)

                    fullinfo = False
                else:
                    # periodically do a reset so we get all information, not just sensor data
                    if itercount > self.iterations:
                        itercount = 0       # reset itercount
                        fullinfo = True

                    for sensor in sensorlist:
                        if self.getportdevicestatus(sensor.sensorport, ipx) == 2:
                            sensor.sensorprocessdata = self.getportsensorprocessdata(sensor.sensorport, ipx)
                        else:
                            sensor.sensorprocessdata = None

                for sensor in sensorlist:
                    sensor.json = sensor.datatojson(ipx)
                    if sensor.json is not None:
                        logging.info('(%s) Writing sensor data points to Influx', ipx)
                        try:
                            client.write_points(sensor.json)
                        except influxdb.exceptions.InfluxDBClientError as excp:
                            logging.error('(%s) Client error writing sensor points - Error (%d)',
                                          ipx, excp.errno)
                        except influxdb.exceptions.InfluxDBClientError as excp:
                            logging.error('(%s) Server error writing sensor points - Error (%d)',
                                          ipx, excp.errno)

                for portnumber in range(0, self.getportcount(ipx) - 1):
                    if savesensorlist is not None:
                        if sensorlist[portnumber] is None:
                            if savesensorlist[portnumber] is not None:
                                mesg = '(' + ipx + ') Sensor on port savesensorlist[portnumber], ' \
                                       'type ' + str(savesensorlist[portnumber].sensortype) + \
                                       ', with serial ' + savesensorlist[portnumber].sensorserial + \
                                       '  not found'
                                evtd = "{'ipaddress': '" + ipx + "', 'event: 'SENSORNOTFOUND', " + \
                                       "'mesg': '" + mesg + "'}"
                                sendtosaas(ipx, client, 'CONFIGURATION', evtd)
                        else:
                            if savesensorlist[portnumber] is None:
                                mesg = '(' + ipx + ') New sensor on port ' + \
                                       str(savesensorlist[portnumber].sensorport) + ' detected, ' \
                                       'type ' + str(savesensorlist[portnumber].sensortype) + \
                                       ', with serial ' + savesensorlist[portnumber].sensorserial
                                evtd = "{'ipaddress': '" + ipx + "', 'event: 'NEWSENSORFOUND', " + \
                                       "'mesg': '" + mesg + "'}"
                                sendtosaas(ipx, client, 'CONFIGURATION', evtd)
                            elif savesensorlist[portnumber].sensortype != sensorlist[portnumber].sensortype:
                                mesg = '(' + ipx + ') Sensor type on port ' + \
                                       str(savesensorlist[portnumber].sensorport) + ' changed, ' \
                                       'type ' + str(savesensorlist[portnumber].sensortype) + \
                                       ', with serial ' + savesensorlist[portnumber].sensorserial
                                evtd = "{'ipaddress': '" + ipx + "', 'event: 'SENSORTYPECHANGED', " + \
                                       "'mesg': '" + mesg + "'}"
                                sendtosaas(ipx, client, 'CONFIGURATION', evtd)
                            elif savesensorlist[portnumber].sensorserial != sensorlist[portnumber].sensorserial:
                                mesg = '(' + ipx + ') Sensor serial on port ' + \
                                       str(savesensorlist[portnumber].sensorport) + ' changed, ' \
                                       'type ' + str(savesensorlist[portnumber].sensortype) + \
                                       ', with serial ' + savesensorlist[portnumber].sensorserial
                                evtd = "{'ipaddress': '" + ipx + "', 'event: 'SENSORSERIALCHANGED', " + \
                                       "'mesg': '" + mesg + "'}"
                                sendtosaas(ipx, client, 'CONFIGURATION', evtd)

                    # check thresholds - property key is vendorid@sensorserial@thresholdtype
                    # get the sensitvities from the previously read properties and convert
                    jsontocheck = sensorlist[portnumber].json
                    if jsontocheck is not None:
                        jsondict = None
                        try:
                            jsondict = jsontocheck[0][ 'fields']
                        except KeyError:
                            break
                        srchserial = sensorlist[portnumber].sensorserial
                        srchvendor = sensorlist[portnumber].sensorvendorid
                        srchroot = str(srchvendor) + '@' + srchserial
                        if srchvendor == 310:  # ifm electronic gmbh
                            if sensorlist[portnumber].sensortype in [416, 417]:       # vibration
                                srchacc = srchroot + '@' + 'acceleration'
                                srchvel = srchroot + '@' + 'velocity'

                                # get the reporting thresholds
                                accelthreshold = 99999.99
                                try:
                                    accelthreshold = float(prr[srchacc])
                                except KeyError:
                                    break

                                velthreshold = 99999.99
                                try:
                                    velthreshold = float(prr[srchvel])
                                except KeyError:
                                    break

                                # get the sensor values
                                acctocheck = 99999.99
                                veltocheck = 99999.99
                                try:
                                    acctocheck = float(jsondict['acceleration'])
                                    veltocheck = float(jsondict['velocity'])
                                except KeyError:
                                    break

                                if acctocheck > accelthreshold:
                                    mesg = '(' + ipx + ') Acceleration value for ' + \
                                           sensorlist[portnumber].sensorname + '/' + \
                                           sensorlist[portnumber].sensorlocalname + \
                                           ' sensor on type on port ' + \
                                           str(sensorlist[portnumber].sensorport) + \
                                           ' exceeds threshold, ' + \
                                           str(acctocheck) + '--' + str(accelthreshold)
                                    evtd = "{'ipaddress': '" + ipx + "', 'event: 'ACCELERATION', " + \
                                           "'mesg': '" + mesg + "'}"
                                    sendtosaas(ipx, client, 'SENSOR', evtd)
                                if veltocheck > velthreshold:
                                    mesg = '(' + ipx + ') Velocity value for ' + \
                                           sensorlist[portnumber].sensorname + '/' + \
                                           sensorlist[portnumber].sensorlocalname + \
                                           ' sensor on type on port ' + \
                                           str(sensorlist[portnumber].sensorport) + \
                                           ' exceeds threshold, ' + \
                                           str(veltocheck) + '--' + str(velthreshold)
                                    evtd = "{'ipaddress': '" + ipx + "', 'event: 'VELOCITY', " + \
                                           "'mesg': '" + mesg + "'}"
                                    sendtosaas(ipx, client, 'SENSOR', evtd)
                            elif sensorlist[portnumber].sensortype in [446]:          # temperature
                                srchtempmax = srchroot + '@' + 'temperaturemax'
                                srchtempmin = srchroot + '@' + 'temperaturemin'

                                # get the reporting thresholds
                                tmaxthreshold = 99999.99
                                try:
                                    tmaxthreshold = float(prr[srchtempmax])
                                except KeyError:
                                    tmaxthreshold = 99999.99

                                tminthreshold = -273.00
                                try:
                                    tminthreshold = float(prr[srchtempmin])
                                except KeyError:
                                    tminthreshold = -273.00

                                # get the sensor value to check against from JSON
                                temptocheck = 0.0
                                try:
                                    temptocheck = float(jsondict['temperature'])
                                except KeyError:
                                    break

                                if temptocheck < tminthreshold:
                                    mesg = '(' + ipx + ') Temperature value for ' + \
                                           sensorlist[portnumber].sensorname + '/' + \
                                           sensorlist[portnumber].sensorlocalname + \
                                           ' sensor on type on port ' + \
                                           str(sensorlist[portnumber].sensorport) + \
                                           ' exceeds minimum threshold, ' + \
                                           str(temptocheck) + '--' + str(tminthreshold)
                                    evtd = "{'ipaddress': '" + ipx + "', 'event: 'TEMPERATUREMIN', " + \
                                           "'mesg': '" + mesg + "'}"
                                    sendtosaas(ipx, client, 'SENSOR', evtd)
                                elif temptocheck > tmaxthreshold:
                                    mesg = '(' + ipx + ') Temperature value for ' + \
                                           sensorlist[portnumber].sensorname + '/' + \
                                           sensorlist[portnumber].sensorlocalname + \
                                           ' sensor on type on port ' + \
                                           str(sensorlist[portnumber].sensorport) + \
                                           ' exceeds maximum threshold, ' + \
                                           str(temptocheck) + '--' + str(tmaxthreshold)
                                    evtd = "{'ipaddress': '" + ipx + "', 'event: 'TEMPERATUREMAX', " + \
                                           "'mesg': '" + mesg + "'}"
                                    sendtosaas(ipx, client, 'SENSOR', evtd)

                savesensorlist = sensorlist     # save sensor state for comparison
                time.sleep(self.duration)       # wait for next execution
        else:
            # no device information to retrieve
            logging.error('(%s) No IO-Link master data to retrieve, no ports found, process not started', ipx)
            return 'NoData'

    def getsensorsondevices(self, ipz):
        """
        Build the list of sensors/devices for the IO-Link Master
        :return: List (portfolio) of sensors/devices
        """
        portefeuille = []
        for portnumber in range(1, self.getportcount(ipz) + 1):
            # if the status for the port is device_connected
            logging.info('(%s) Getting device/sensor information for IOLM, port = %d', ipz, portnumber)

            if self.getportdevicestatus(portnumber, ipz) == 2:
                logging.info("(%s) Port %d: sensor present, retrieving sensor details", ipz, portnumber)
                # CHECK IF THIS PORT WAS ACTIVE LAST ITERATION
                # get the sensor information and add it to the device sensor list
                sensex = IOLinkSensorInformation(portnumber,
                                                 self.getportsensordeviceid(portnumber, ipz),
                                                 self.getportsensorlocalname(portnumber, ipz),
                                                 self.getportsensorserial(portnumber, ipz),
                                                 self.getportsensorvendorid(portnumber, ipz),
                                                 self.getportsensorname(portnumber, ipz),
                                                 self.getportsensorprocessdata(portnumber, ipz))
            else:
                logging.info('(%s) Port %d: no sensor connected', ipz, portnumber)
                # CHECK IF THIS PORT WAS INACTIVE LAST ITERATION
                # sensor not present
                sensex = IOLinkSensorInformation(portnumber, '', '', '', '', '', None)

            # add the sensor to the sensor list
            logging.info('(%s) Inserting sensor/device into IO-Link master structure for port %d',
                         ipz, portnumber)
            portefeuille.insert(portnumber, sensex)
        return portefeuille

    def check(self):
        """
        Validate properties file entries are not defaulted
        :return: False, if any of delay devicename or port are defaulted
        """
        if self.delay == -1 or self.devicename == '' or self.ipaddress == '' or self.port == -1:
            return False
        else:
            return True

    # Master level (IO-Link interface device) methods
    def getbootloaderrevision(self, ipz):
        """
        manufacturers boot loader revision for the IO-Link master
        :return: JSON bootloader revision
        """
        logging.info('(%s) Retrieving the IO-Link boot loader revision', ipz)
        return self.httpget('/deviceinfo/bootloaderrevision/getdata', ipz)

    def getdevicefamily(self, ipz):
        """
        manufacturers device family name
        :return: JSON manufacturers device family name
        """
        logging.info('(%s) Retrieving the IO-Link master device family', ipz)
        return self.httpget('/deviceinfo/devicefamily/getdata', ipz)

    def getextensionrevisions(self, ipz):
        """
        manufacturers extension revisions for the IO-Link master
        :return: JSON extension revisions
        """
        logging.info('(%s) Retrieving the IO-Link master extensions revision', ipz)
        return self.httpget('/deviceinfo/extensionrevisions/getdata', ipz)

    def gethwrevision(self, ipz):
        """
        manufacturers hardware revision for the IO-Link master
        :return: JSON hardware revision
        """
        logging.info('(%s) Retrieving the IO-Link master hardware revision', ipz)
        return self.httpget('/deviceinfo/hwrevision/getdata', ipz)

    def getproductcode(self, ipz):
        """
        manufacturers name for the IO-Link master
        :return: JSON manufacturers name for device
        """
        logging.info('(%s) Retrieving the IO-Link master manufacturer device name', ipz)
        return self.httpget('/deviceinfo/productcode/getdata', ipz)

    def getproductserial(self, ipz):
        """
        Manufacturers serial number for the IO-Link master
        :return: IO-Link master serial number
        """
        logging.info('(%s) Retrieving the IO-Link master serial number', ipz)
        return self.httpget('/deviceinfo/serialnumber/getdata', ipz)

    def getswrevision(self, ipz):
        """
        manufacturers software revision for the IO-Link master
        :return: JSON software revision
        """
        logging.info('(%s) Retrieving the IO-Link master software revision', ipz)
        return self.httpget('/deviceinfo/swrevision/getdata', ipz)

    def getvendor(self, ipz):
        """
        manufacturers name
        :return: JSON manufacturer's name
        """
        logging.info('(%s) Retrieving the IO-Link master manufacturer name', ipz)
        return self.httpget('/deviceinfo/vendor/getdata', ipz)

    def gettree(self, ipz):
        """
        # JSON structure details of IO-Link Master interface
        :return: JSON capabilities tree of device
        """
        logging.info('Retrieving IO-Link tree of services')
        return self.httpget('/gettree', ipz, False)

    # IO-Link master port specific data

    def getportsensorlocalname(self, xport, ipz):
        """
        logical name of the device/sensor, assigned during setup
        :param xport: port number
        :param ipz: ip address string
        :return: device/sensor name
        """
        logging.info('(%s) Retrieving sensor local name for port %d', ipz, xport)
        return self.httpget('/iolinkmaster/port[' + str(xport) +
                            ']/iolinkdevice/applicationspecifictag/getdata', ipz)

    def getportsensordeviceid(self, xport, ipz):
        """
        sensor numerical device type
        :param xport: port number
        :param ipz: ip address string
        :return: device type
        """
        logging.info('(%s) Retrieving sensor numerical device type for port %d', ipz, xport)
        return self.httpget('/iolinkmaster/port[' + str(xport) +
                            ']/iolinkdevice/deviceid/getdata', ipz)

    def getportsensorprocessdata(self, xport, ipz):
        """
        Get the process data from the sensor on specified port
        :param xport: port number
        :param ipz: ip address string
        :return: sensor data
        """
        logging.info('(%s) Getting process data from sensor on port %d', ipz, xport)
        return self.httpget('/iolinkmaster/port[' + str(xport) +
                            ']/iolinkdevice/pdin/getdata', ipz)

    def getportsensorname(self, xport, ipz):
        """
        device/sensor manufacturer's business name
        :param xport: port number
        :param ipz: ip address string
        :return: manufacturer's business name
        """
        logging.info('(%s) Retrieving the sensor manufacturers name for port %d', ipz, xport)
        return self.httpget('/iolinkmaster/port[' + str(xport) +
                            ']/iolinkdevice/productname/getdata', ipz)

    def getportsensorserial(self, xport, ipz):
        """
        get the device/sensor serial number
        :param xport: port number
        :param ipz: ip address string
        :return: device serial number
        """
        logging.info('(%s) Retrieving the sensor serial number for port %d', ipz, xport)
        return self.httpget('/iolinkmaster/port[' + str(xport) +
                            ']/iolinkdevice/serial/getdata', ipz)

    def getportdevicestatus(self, xport, ipz):
        """
        status of the port/device on the IO-Link master
        :param xport: port number
        :param ipz: ip address string
        :return: port status
        0 = SENSOR_NOT_CONNECTED
        1 = SENSOR_IN_PREOPERATE
        2 = SENSOR_IN_OPERATE
        3 = SENSOR_WRONG
        """
        logging.info('(%s) Retrieving the port status for port %d', ipz, xport)
        return self.httpget('/iolinkmaster/port[' + str(xport) +
                            ']/iolinkdevice/status/getdata', ipz)

    def getportsensorvendorid(self, xport, ipz):
        """
        device/sensor vendor id
        :param xport: port number
        :param ipz: ip address string
        :return: vendor id for device manufacturer
        """
        logging.info('(%s) Retrieving the sensor vendor id for port %d', ipz, xport)
        return self.httpget('/iolinkmaster/port[' + str(xport) +
                            ']/iolinkdevice/vendorid/getdata', ipz)

    def getportpin2in(self, xport, ipz):
        """
        presence (1) or absence (0) of a sensor on the port
        :param xport: port number
        :param ipz: ip address string
        :return: value of pin 2 on the device interface
        """
        logging.info('(%s) Retrieving the sensor pin2in value for port %d', ipz, xport)
        return self.httpget('/iolinkmaster/port[' + xport +
                            ']/pin2in/getdata', ipz)

    # Device address data

    def getipaddress(self, ipz):
        """
        IP address of the IO-Link master device
        :param ipz: ip address string
        :return:
        """
        logging.info('(%s) Retrieving the IO-Link master IP address', ipz)
        return self.httpget('/iotsetup/network/ipaddress/getdata', ipz)

    def getmacaddress(self, ipz):
        """
        MAC address of the IO-Link master device
        :param ipz: ip address string
        :return:
        """
        logging.info('(%s) Retrieving the IO-Link master MAC address', ipz)
        return self.httpget('/iotsetup/network/macaddress/getdata', ipz)

    # IO-link master process data

    # Current being drawn by the IO-Link Master device
    def getmilliamperes(self, ipz):
        """
        Current being drawn by the IO-Link master device
        :param ipz: ip address string
        :return: current in milliamperes
        """
        logging.info('(%s) Retrieving IO-Link master current drawn', ipz)
        return self.httpget('/processdatamaster/current/getdata', ipz)

    def getsupervisionstatus(self, ipz):
        """
        Get the supervisory status of the IO-Link master
        :param ipz: ip address string
        :return: operating state of the IO-Link master
        0 = NO ERROR
        1 = SHORT CIRCUIT
        2 = OVERLOAD
        3 = UNDERVOLTAGE
        """
        logging.info('(%s) Retrieving the IO-Link master supervision status', ipz)
        return self.httpget('/processdatamaster/supervisionstatus/getdata', ipz)

    def gettemperature(self, ipz):
        """
        Temperature of the IO-Link master device
        :param ipz: ip address string
        :return: celsius temperature
        """
        logging.info('(%s) Retrieving the IO-Link master termperature', ipz)
        return self.httpget('/processdatamaster/temperature/getdata', ipz)

    def getvolts(self, ipz):
        """
        Supply voltage (DC) applied to IO-Link Master device
        :param ipz: ip address string
        :return: voltage across the IO-Link master device
        """
        logging.info('(%s) Retrieving the IO-Link master voltage', ipz)
        return self.httpget('/processdatamaster/current/getdata', ipz)

    def getportcount(self, ipz):
        """
        get the number of ports on the IO-Link master
        :param ipz: ip address string
        :return: number of ports
        """
        # get the device capabilities by JSON descent, determine total sensor ports
        logging.info('(%s) Getting the services available for this IO-Link master', ipz)
        capabilities = self.gettree(ipz)
        if capabilities is not None:
            # navigate tree structure to get embedded list of ports
            itemdict = json.loads(capabilities)
            itemdata = itemdict['data']
            itemsubs = itemdata['subs']
            for itemsub in itemsubs:
                if itemsub['identifier'] == 'iolinkmaster':
                    logging.info('(%s) Return number of ports from description', ipz)
                    return len(itemsub['subs'])
            logging.info('(%s) Return default, zero ports found', ipz)
            return 0
        else:
            return None

    def httpget(self, requeststring, ipz, processed=True, tmout=3):
        """
        Query IO-Link master device via HTTP Get
        :param requeststring: URL to query
        :param ipz: ip address string
        :param processed: True, if JSON processing is needed
        :param tmout: HTTP request timeout value as float
        :return: response from HTTP request
        """
        # Construct query URL
        url = 'http://' + self.ipaddress + requeststring
        myresponse = None

        # unsecured request, no credentials required
        try:
            logging.info('(%s) Sending Get request to IO-Link master - %s', ipz, url)
            myresponse = requests.get(url, timeout=tmout)
        except requests.exceptions.ConnectTimeout:
            logging.error('(%s) HTTP Get failed (Connection Timeout) for %s', ipz, url)
        except requests.exceptions.ConnectionError:
            logging.error('(%s) HTTP Get failed (Connection Error) for %s', ipz, url)

        if myresponse is None:
            logging.error('(%s) No response from HTTP API, endpoint may be down %s', ipz, url)
            raise ConnectionError('No response from HTTP API, endpoint may be down')

        # For successful API call, response code will be 200 (OK)
        # IO-Link has other return codes in the range of 200-299 we will not handle
        if myresponse.ok:
            # Loading the response data into a dict variable
            scontent = myresponse.content.decode('utf-8')
            if processed:
                logging.info('(%s) Processing response from HTTP into JSON structure', ipz)
                jdata = json.loads(scontent)
                dval = jdata['data']
                vval = dval['value']
                return vval
            else:
                logging.info('(%s) Returning HTTP response in raw form', ipz)
                return scontent
        else:
            # If response code is not ok (200), print the resulting http error code with description
            logging.error('(%s) HTTP response error (%d), on %s', ipz, myresponse.status_code(), requeststring)
            return None


class Database(object):
    """
    Interface to InfluxDB database
    """

    def __init__(self, ipx, gsd):
        """
        Instance initializer for the Database class
        :param ipx: IP address string
        :param gsd: global settings handle
        """
        try:
            self.client = InfluxDBClient(host=gsd.influxhost, port=gsd.influxport)
        except requests.exceptions.ConnectionError as excp:
            logging.error('(%s) InfluxDB DB connect failed (Error: %d)', ipx, excp.errno)
            return

        # check for existence of our database and create if needed
        dblist = self.client.get_list_database()
        dbexists = False
        logging.info('(%s) Checking for existence of InfluxDB sensor database', ipx)
        for dbentry in dblist:
            if dbentry.get('name') == gsd.influxdatabase:
                dbexists = True
                logging.info('(%s) InfluxDB sensor database found', ipx)

        if not dbexists:
            logging.info('(%s) Creating InfluxDB sensor database', ipx)
            self.client.create_database(gsd.influxdatabase)

    def connect(self, ipx, gsc):
        """
        Connect to the sensor database
        :param ipx: IP address string
        :param gsc: Global settings
        :return: database connect
        """
        try:
            self.client.switch_database(gsc.influxdatabase)
        except requests.exceptions.ConnectionError as excp:
            logging.error('(%s) InfluxDB schema connect failed (Error: %d)', ipx, excp.errno)
            return None

        return self.client


class IOLinkMasterList(object):
    """
    Models the properties of an IO-Link master interface device
    """

    def __init__(self, raw_device_list=None, dp=None):
        """
        Instance initializer for the IOLinkMasterList class
        :param raw_device_list: unprocess list of IOLMs
        :param dp: Dictionary of properties
        """

        # self.configfile = 'sensorconfig.pkl'
        if raw_device_list is not None:
            # parse list of devices into formal list
            self.devices = raw_device_list.split(',')
            self.devicelist = []

            if len(self.devices) < 1:
                logging.error('No devices found in the devicelist')
                raise Exception('No devices found in devicelist')
            else:
                # for each of the list devices set up definitions
                logging.info('Processing the list of devices in devicelist')
                for device in self.devices:
                    tempdevice = IOLinkMaster()
                    tempdevice.devicename = device
                    logging.info('Processing the properties loaded')
                    for key in dp:
                        # check if the current prop is related to the current device
                        searchstring = device + '.'
                        locn = key.find(searchstring, 0)
                        if locn != -1:
                            # property is of interest
                            logging.info('Property located in dictionary for current device')
                            myprop = key[key.find('.', 0) + 1:]
                            if myprop == 'ipaddress':
                                tempdevice.ipaddress = str(resolveipaddress(dp[key]))
                                tempdevice.configfile = tempdevice.ipaddress
                                logging.info('ipaddress property located: %s', tempdevice.ipaddress)
                            elif myprop == 'port':
                                tempdevice.port = dp[key]
                                logging.info('port property located %s', tempdevice.port)
                            elif myprop == 'delay':
                                tempdevice.delay = dp[key]
                                logging.info('delay property located %s', tempdevice.delay)
                                # check the value and split it and validate
                                delayparts = tempdevice.delay.partition(',')
                                # part 0 = duration, part 1 = separator, part 2 = iterations
                                if not isanint(delayparts[0]):
                                    logging.error('Invalid delay value found in properties for %s, both defaulted',
                                                  device)
                                    tempdevice.duration = 10        # set suitable wait for cycle
                                    tempdevice.iterations = 6       # set suitable loops for full info
                                else:
                                    tempvalue = int(delayparts[0])
                                    if tempvalue < 5 or tempvalue > 600:
                                        logging.error('Out of range delay value, for %s, both defaulted',
                                                      device)
                                        tempdevice.duration = 10
                                        tempdevice.iterations = 6
                                    else:
                                        tempdevice.duration = tempvalue
                                        if len(delayparts[1]) < 1:
                                            logging.error('Incomplete delay value found in properties for %s, ' +
                                                          ' iterations defaulted',
                                                          device)
                                            tempdevice.iterations = 6
                                        elif len(delayparts[2]) < 1:
                                            logging.error(
                                                'Iterations value not found in properties for %s, iterations defaulted',
                                                device)
                                            tempdevice.iterations = 6
                                        else:
                                            if not isanint(delayparts[2]):
                                                logging.error(
                                                    'Non-integer iterations value found in properties for %s, ' +
                                                    'iterations defaulted',
                                                    device)
                                                tempdevice.iterations = 6
                                            else:
                                                tempvalue = int(delayparts[2])
                                                if tempvalue < 5 or tempvalue > 20:
                                                    logging.error(
                                                        'Out of range iterations value found in properties for %s, ' +
                                                        'iteration defaulted',
                                                        device)
                                                    tempdevice.iterations = 6
                                                else:
                                                    tempdevice.iterations = tempvalue
                            else:
                                logging.error('Unrecognized property found %s', myprop)
                                raise Exception('Unrecognized property found {}'.format(myprop))
                        else:
                            # skip the property name
                            logging.info('Propety not required, skipped')
                    if not (tempdevice.check()):
                        logging.error('Incomplete configuration on device %s', device)
                        raise Exception('Incomplete configuration on device {}'.format(device))
                    else:
                        logging.info('Appending device to devicelist')
                        self.devicelist.append(tempdevice)


class IOLinkSensorInformation:
    """
    Models the properties of an IO-Link sensor
    """

    def __init__(self, prt=None, typ=None, nm=None, ser=None, vid=None, lnm=None, pd=None):
        """
        Initializer for IO-Link sensor objects, handles no argument case as well
        :param prt: Port IP port
        :param typ: Type sensor type
        :param nm: sensor name
        :param ser: sensor Serial number
        :param vid: sensor Vendor id
        :param lnm: sensor local name
        :param pd: sensor process data
        """
        if prt is None:
            self.sensorport = -1
            self.sensortype = ''
            self.sensorname = ''
            self.sensorserial = ''
            self.sensorid = ''
            self.sensorvendorid = ''
            self.sensordata = -1
            self.sensorlocalname = ''
            self.sensorprocessdata = None
            self.json = ''
        else:
            self.sensorport = prt
            self.sensortype = typ
            self.sensorname = nm
            self.sensorserial = ser
            self.sensorvendorid = vid
            self.sensordata = -2
            self.sensorlocalname = lnm
            self.sensorprocessdata = pd
            self.json = ''

    def datatojson(self, ipx):
        """
        Taking into account the sensor type, the sensor data is translated to a JSON definition
        to allow for multi-part results
        :param ipx: IP address of IOLM
        :return: JSON formatted sensor data
        """
        if self.sensorvendorid == 310:          # ifm electronic gmbh
            if self.sensortype in [416, 417]:   # JN2201, JN2202
                configuration = int(self.sensorprocessdata[10:11], 16)
                diagnosis = int(self.sensorprocessdata[8:10], 16)
                velocity = int(self.sensorprocessdata[4:8], 16) / 100.0
                acceleration = int(self.sensorprocessdata[0:4], 16) / 100.0
                logging.info('(%s) Sensor data conversion for %s - %s', ipx,
                             self.sensorlocalname, self.sensorname)
                data = [dict(measurement='vibrationEvents',
                             tags={'iolmip': ipx, 'sensorvendorid': self.sensorvendorid,
                                   'sensorserial': self.sensorserial, 'sensorport': self.sensorport,
                                   'sensorlocalname': self.sensorlocalname,
                                   'sensorname': self.sensorname, 'sensortype': self.sensortype},
                             time=datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                             fields={'configuration': configuration, 'diagnosis': diagnosis,
                                     'velocity': velocity, 'acceleration': acceleration})]
                return data
            elif self.sensortype in [446]:      # TA2002, TA2012, TA2212, TA2232, TA2242, TA2262,
                                                # TA2292, TA2502, TA2512, TA2532, TA2502, TA2512
                                                # TA2532, TA2542, TA2792, TA2802, TA2812, TA2832,
                                                # TA2842
                temperature = int(self.sensorprocessdata, 16) / 10.0
                logging.info('(%s) Sensor data conversion for %s - %s', ipx,
                             self.sensorlocalname, self.sensorname)

                data = [dict(measurement='temperatureEvents',
                             tags={'iolmip': ipx, 'sensorvendorid': self.sensorvendorid,
                                   'sensorserial': self.sensorserial, 'sensorport': self.sensorport,
                                   'sensorlocalname': self.sensorlocalname,
                                   'sensorname': self.sensorname, 'sensortype': self.sensortype},
                             time=datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                             fields={'temperature': temperature})]
                return data
            elif self.sensortype in [400]:      # PN7571, PN7071
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [401]:      # PN7592, PN7572, PN7092, PN7072
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [403]:      # PN7594, PN7094
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [463]:      # PN2094, PN2594
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [402]:      # PN7593, PN7093
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [431]:      # PN3094, PN3594
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [399]:      # PN7570, PN7070
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [428]:      # PN3071, PN3571
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [429]:      # PN3092, PN3592
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [404]:      # PN7596, PN7096
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [454]:      # PN7694, PN7294
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [462]:      # PN2093, PN2593,
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [430]:      # PN3093, PN3593
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [461]:      # PN2092, PN2592
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [427]:      # PN3070, PN3570
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [406]:      # PN7599, PN7099
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [459]:      # PN2070, PN2570
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [473]:      # PN2294, PN2694
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [312]:      # PN7006, PE7006, PN016A
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [311]:      # PN7004, PE7004, PN014A
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s', ipx,
                              self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [157]:      # PI2794, PY2794, PI2894, PI2204, PI2214
                                                # PI2304
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [257]:      # PI2798, PI2898
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [256]:      # PI2797, PI2897, PI2207, PI2307
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [156]:      # PI2793, PI2893, PI2203, PI2303
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [259]:      # PI2789, PI2889
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [258]:      # PI2799, PI2899, PI2209, PI2309
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [159]:      # PI2796, PI2896, PI2206, PI2306
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [158]:      # PI2795, PI2895, PI2205, PI2305
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [4]:        # PP7552, PP002E
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [3]:        # PP7551, PP001E
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [6]:        # PP7554, PP004E
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [713]:      # PV7004
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [710]:      # PV7002
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [709]:      # PV7001
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [712]:      # PV7003
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [711]:      # PV7023
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [5]:        # PP7553, PP003E
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [708]:      # PV7000
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [2]:        # PP7550, PP000E
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [7]:        # PP7556
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [855]:      # PV7604
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [853]:      # PV7602
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [899]:      # PV7623
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [854]:      # PV7603
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [852]:      # PV7601
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [851]:      # PV7600
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [367]:      # PQ3834
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [366]:      # PQ3809
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [575]:      # SM8000, SM8100, SM84000, SM8500
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [572]:      # SM7000, SM7100, SM7400, SM7500
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [391]:      # SM9000, SM9100, SM9400, SM9500
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [569]:      # SM6000, SM6100, SM6400, SM6500
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [389]:      # SM2000, SM2100, Sm2400, SM2500
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [390]:      # SM2001, SM2601
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [392]:      # SM9001, SM9601
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [576]:      # SM8001, SM8601
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [570]:      # SM6001, SM6601
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [671]:      # SM4000, SM4100
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [671]:      # SM4000, SM4100, PN7092, PN7072
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [509]:      # SM0510
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [577]:      # SM8050
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [573]:      # SM7001, SM7601
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [484]:      # SV4200, SV4500
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [488]:      # SV5200, SV5500
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [484]:      # SV4200, SV4500
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [492]:      # SV7200, SV7500
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [494]:      # SV7610
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [486]:      # SV4610
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [490]:      # SV5610
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            elif self.sensortype in [533]:      # SA5030, SA5040, SA2000, SA5000,
                                                # SA4100, SA4300
                logging.error('(%s) Unhandled sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None
            else:
                logging.error('(%s) Unrecongnized sensor type (%d) conversion for %s - %s',
                              ipx, self.sensortype, self.sensorlocalname, self.sensorname)
                return None


def isanint(trystring):
    """
    Checks a string to see if it is a valid integer via cast and exception
    :param trystring: string to check
    :return: True, if the string evaluates to a valid integer
    """
    try:
        int(trystring)
        return True
    except ValueError:
        return False


def sendtosaas(ipx, clnt, evt, evtdata):
    """
    Sends message to SaaS for trust
    :param ipx: IP address of IOLM
    :param clnt: InfluxDB client connection
    :param evt: event type
    :param evtdata: event data as JSON list
    :return: None
    """
    # {'event_type': 'code', 'data': <event-type specific data>}
    # '{"x": 123, "y": -1, "z": 5}'
    proc = subprocess.Popen(['/opt/corlina/bin/api/get-eventstamp',
                             '--etype', evt,
                             '--data', evtdata],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    eventstamp_json, error = proc.communicate()
    status_code = proc.wait()

    # error handling:
    if status_code != 0:
        saaserror = format(error)
        logging.error('(%s) SaaS event write error, error = %s, event = %s, event data = %s',
                      ipx, saaserror, evt, evtdata)
    else:
        # success path:
        eventstamp = json.loads(eventstamp_json.decode('utf-8'))
        agentuuid = 'Agent UUID: {}'.format(eventstamp['uuid'])
        eventstamp = 'Eventstamp: {}'.format(eventstamp['eventstamp'])
        logging.info('(%s) Writing SaaS result points to Influx', ipx)
        data = [
            dict(measurement="saasResults",
                 tags={'iolmip': ipx, 'agentuuid': agentuuid, 'eventstamp': eventstamp},
                 time=datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                 fields=dict(event=evt, eventdata=evtdata))
        ]
        try:
            clnt.write_points(data)
        except influxdb.exceptions.InfluxDBClientError as excp:
            logging.error('(%s) Client error writing IOLM points - Error (%d)',
                          ipx, excp.errno)
        except influxdb.exceptions.InfluxDBClientError as excp:
            logging.error('(%s) Server error writing IOLM points - Error (%d)',
                          ipx, excp.errno)


def resolveipaddress(ip2check):
    """
    Checks a IP V4 address for validity
    :param ip2check: IP address to check, either octet or symbolic form
    :return: IP address as an octet string if valid or empty string if not
    """
    # check if address is only digits 0-9 and , characters
    isnumericip = re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip2check)

    # if we have a numeric IP address (octet form)
    if isnumericip:
        addrx = ''
        try:
            parts = ip2check.split('.')
            # must have 4 parts and each part must be between 0 and 255
            if not (len(parts) == 4 and all(0 <= int(part) < 256 for part in parts)):
                return addrx
        except ValueError:
            return addrx  # one of the 'parts' not convertible to integer
        except (AttributeError, TypeError):
            return addrx  # `ip` isn't even a string

        try:
            addrx = IP(ip2check)
            logging.info('Verified %s as a correct octet IP address', ip2check)
        except ValueError:
            logging.error('Invalid IP octet address provided, %s', ip2check)
        return addrx
    else:
        # symbolic address we have to look up
        ip2use = ''
        try:
            ip2use = socket.gethostbyname(ip2check)
        except socket.error:
            logging.error('Invalid IP address provided %s', ip2check)

        return ip2use


if __name__ == "__main__":
    pfm = sys.argv[0]
    pfb = os.path.basename(pfm)
    # load capture properties
    prx = Properties().load()
    # handle global configuration properties
    gsx = GlobalSettings(prx)

    if len(gsx.devicelist) < 1:  # no devices
        logging.error('No devices specified in the device list')
        print('No devices specified in the device list')
        iolmlistm = None
        sys.exit(3)
    else:
        logging.info('IO-Link master processing initiated')
        print('IO-Link master processing initiated')
        iolmlistn = IOLinkMasterList(gsx.devicelist, prx)

        # we will drop out if there are no iolm entries
        if iolmlistn.devicelist.__len__() > 0:
            for iolmdev in iolmlistn.devicelist:
                # need spawn a process to handle the IO-Link master
                iolmdev.proc = multiprocessing.Process(target=iolmdev.getreadings,
                                                       args=(iolmdev.ipaddress, gsx, prx))
                iolmdev.proc.start()
        else:
            logging.info('No Processes to initiate for IO-Link masters')
            print('No Processes to initiate for IO-Link masters')
            sys.exit(4)

        while True:
            time.sleep(10)

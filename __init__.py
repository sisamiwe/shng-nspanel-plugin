#!/usr/bin/env python3
# vim: set encoding=utf-8 tabstop=4 softtabstop=4 shiftwidth=4 expandtab
#########################################################################
#  Copyright 2020-      <AUTHOR>                                  <EMAIL>
#########################################################################
#  This file is part of SmartHomeNG.
#  https://www.smarthomeNG.de
#  https://knx-user-forum.de/forum/supportforen/smarthome-py
#
#  Sample plugin for new plugins using MQTT to run with SmartHomeNG
#  version 1.7 and upwards.
#
#  SmartHomeNG is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  SmartHomeNG is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with SmartHomeNG. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

from datetime import datetime, timedelta
import time
import yaml
import queue


from lib.model.mqttplugin import MqttPlugin
from .webif import WebInterface

from lib.item import Items
items = Items.get_instance()


class NSPanel(MqttPlugin):
    """
    Main class of the Plugin. Does all plugin specific stuff and provides
    the update functions for the items
    """

    TEMP_SENSOR = ['ANALOG', 'ESP32']
    TEMP_SENSOR_KEYS = {'Temperature1': 'item_temp_analog', 'Temperature': 'item_temp_esp32'}

    PLUGIN_VERSION = '1.0.0'

    def __init__(self, sh):
        """
        Initializes the plugin.

        """

        # Call init code of parent class (MqttPlugin)
        super().__init__()
        if not self._init_complete:
            return

        # get the parameters for the plugin (as defined in metadata plugin.yaml):
        try:
            self.webif_pagelength = self.get_parameter_value('webif_pagelength')
            self.tasmota_topic = self.get_parameter_value('topic')
            self.telemetry_period = self.get_parameter_value('telemetry_period')
            self.config_file_location = self.get_parameter_value('config_file_location')
            pass
        except KeyError as e:
            self.logger.critical("Plugin '{}': Inconsistent plugin (invalid metadata definition: {} not defined)".format(self.get_shortname(), e))
            self._init_complete = False
            return

        # create full_topic
        self.full_topic = self.get_parameter_value('full_topic').lower()
        if self.full_topic.find('%prefix%') == -1 or self.full_topic.find('%topic%') == -1:
            self.full_topic = '%prefix%/%topic%/'
        if self.full_topic[-1] != '/':
            self.full_topic += '/'

        # define properties
        self.current_page = 0
        self.tasmota_devices = {}
        self.custom_msg_queue = queue.Queue(maxsize=50)  # Queue containing last 50 messages containing "CustomRecv"
        self.alive = None

        # read panel config file
        try:
            self.panel_config = self._parse_config_file()
        except Exception as e:
            self.logger.warning(f"Exception during parsing of page config yaml file occurred: {e}")
            self._init_complete = False
            return

        # Add subscription to get device discovery
        self.add_subscription(        'tasmota/discovery/+/config',          'dict',                                    callback=self.on_mqtt_discovery_message)
        self.add_subscription(        'tasmota/discovery/+/sensors',         'dict',                                    callback=self.on_mqtt_discovery_message)
            # self.add_tasmota_subscription('tasmota', 'discovery', '#',           'dict',                                    callback=self.on_mqtt_discovery_message)
        # Add subscription to get device LWT
        self.add_tasmota_subscription('tele', self.tasmota_topic, 'LWT',     'bool', bool_values=['Offline', 'Online'], callback=self.on_mqtt_lwt_message)
        # Add subscription to get device status
        self.add_tasmota_subscription('stat', self.tasmota_topic, 'STATUS0', 'dict',                                    callback=self.on_mqtt_status0_message)
        # Add subscription to get device actions results
        self.add_tasmota_subscription('stat', self.tasmota_topic, 'RESULT',  'dict',                                    callback=self.on_mqtt_message)
        self.add_tasmota_subscription('stat', self.tasmota_topic, 'POWER',   'num',                                     callback=self.on_mqtt_power_message)
        self.add_tasmota_subscription('stat', self.tasmota_topic, 'POWER1',  'num',                                     callback=self.on_mqtt_power_message)
        self.add_tasmota_subscription('stat', self.tasmota_topic, 'POWER2',  'num',                                     callback=self.on_mqtt_power_message)
        self.add_tasmota_subscription('tele', self.tasmota_topic, 'STATE',   'dict',                                    callback=self.on_mqtt_message)
        self.add_tasmota_subscription('tele', self.tasmota_topic, 'SENSOR',  'dict',                                    callback=self.on_mqtt_message)
        self.add_tasmota_subscription('tele', self.tasmota_topic, 'RESULT',  'dict',                                    callback=self.on_mqtt_message)
        self.add_tasmota_subscription('tele', self.tasmota_topic, 'INFO3',   'dict',                                    callback=self.on_mqtt_message)
        self.add_tasmota_subscription('stat', self.tasmota_topic, 'POWER',   'num',                                     callback=self.on_mqtt_power_message)
        self.add_tasmota_subscription('stat', self.tasmota_topic, 'POWER1',  'num',                                     callback=self.on_mqtt_power_message)
        self.add_tasmota_subscription('stat', self.tasmota_topic, 'POWER2',  'num',                                     callback=self.on_mqtt_power_message)

        # init WebIF
        self.init_webinterface(WebInterface)

        return

    def run(self):
        """
        Run method for the plugin
        """
        self.logger.debug("Run method called")

        # start subscription to all topics
        self.start_subscriptions()

        # link items from config to method 'update_item'
        self._get_items_of_panel_config_to_update_item()

        # add scheduler for cyclic time and date update
        dt = self.shtime.now() + timedelta(seconds=20)
        self.scheduler_add('update_nspanel_time', self.send_current_time, next=dt, cycle=60)
        self.scheduler_add('update_nspanel_date', self.send_current_date, cron='1 0 0 * * *', next=dt)

        # set plugin alive
        self.alive = True

        self.send_mqtt_from_nspanel(1)

    def stop(self):
        """
        Stop method for the plugin
        """
        self.logger.debug("Stop method called")
        self.alive = False

        # stop subscription to all topics
        self.stop_subscriptions()

    def parse_item(self, item):
        """
        Default plugin parse_item method. Is called when the plugin is initialized.
        The plugin can, corresponding to its attribute keywords, decide what to do with
        the item in future, like adding it to an internal array for future reference
        :param item:    The item to process.
        :return:        If the plugin needs to be informed of an items change you should return a call back function
                        like the function update_item down below. An example when this is needed is the knx plugin
                        where parse_item returns the update_item function when the attribute knx_send is found.
                        This means that when the items value is about to be updated, the call back function is called
                        with the item, caller, source and dest as arguments and in case of the knx plugin the value
                        can be sent to the knx with a knx write function within the knx plugin.
        """
        if self.has_iattr(item.conf, 'foo_itemid'):
            self.logger.debug(f"parse item: {item.property.path}")

            # subscribe to topic for relay state
            # mqtt_id = self.get_iattr_value(item.conf, 'foo_itemid').upper()
            # payload_type = item.property.type
            # topic = 'shellies/shellyplug-' + mqtt_id + '/relay/0'
            # bool_values = ['off','on']
            # self.add_subscription(topic, payload_type, bool_values, item=item)

            # alternative:
            #   self.add_subscription(topic, payload_type, bool_values, callback=self.on_mqtt_message)
            # and implement callback:
            #   def on_mqtt_message(self, topic, payload, qos=None, retain=None):

            # todo
            # if interesting item for sending values:
            #   return self.update_item

            # if the item is changed in SmartHomeNG and shall update the mqtt device, enable:
            # return self.update_item

    def parse_logic(self, logic):
        """
        Default plugin parse_logic method
        """
        if 'xxx' in logic.conf:
            # self.function(logic['name'])
            pass

    def update_item(self, item, caller=None, source=None, dest=None):
        """
        Item has been updated

        This method is called, if the value of an item has been updated by SmartHomeNG.
        It should write the changed value out to the device (hardware/interface) that
        is managed by this plugin.

        :param item: item to be updated towards the plugin
        :param caller: if given it represents the callers name
        :param source: if given it represents the source
        :param dest: if given it represents the dest
        """
        if self.alive and caller != self.get_shortname():
            # code to execute if the plugin is not stopped
            # and only, if the item has not been changed by this this plugin:
            self.logger.info(f"Update item: {item.property.path}, item has been changed outside this plugin")

            if self.has_iattr(item.conf, 'foo_itemtag'):
                self.logger.debug(
                    f"update_item was called with item {item.property.path} from caller {caller}, source {source} and dest {dest}")
            pass

    ################################
    # CallBacks
    ################################

    def on_mqtt_discovery_message(self, topic: str, payload: dict, qos: int = None, retain: bool = None) -> None:
        """
        Callback function to handle received discovery messages

        :param topic:       MQTT topic
        :param payload:     MQTT message payload
        :param qos:         qos for this message (optional)
        :param retain:      retain flag for this message (optional)

        """

        # tasmota/discovery/0CDC7E31E4CC/config {"ip":"192.168.178.67","dn":"Tasmota","fn":["Tasmota","",null,null,null,null,null,null],"hn":"NSPanel1-1228","mac":"0CDC7E31E4CC","md":"NSPanel","ty":0,"if":0,"ofln":"Offline","onln":"Online","state":["OFF","ON","TOGGLE","HOLD"],"sw":"12.2.0","t":"NSPanel1","ft":"%prefix%/%topic%/","tp":["cmnd","stat","tele"],"rl":[1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],"swc":[-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1],"swn":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null],"btn":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],"so":{"4":0,"11":0,"13":0,"17":0,"20":0,"30":0,"68":0,"73":0,"82":0,"114":0,"117":0},"lk":0,"lt_st":0,"sho":[0,0,0,0],"sht":[[0,0,0],[0,0,0],[0,0,0],[0,0,0]],"ver":1}
        # tasmota/discovery/0CDC7E31E4CC/sensors {"sn":{"Time":"2022-11-28T16:17:32","ANALOG":{"Temperature1":23.2},"ESP32":{"Temperature":28.9},"TempUnit":"C"},"ver":1}

        try:
            (tasmota, discovery, device_id, msg_type) = topic.split('/')
            self.logger.info(f"on_mqtt_discovery_message: device_id={device_id}, type={msg_type}, payload={payload}")
        except Exception as e:
            self.logger.error(f"received topic {topic} is not in correct format. Error was: {e}")
            return

        if msg_type == 'config':
            """
            device_id = 2CF432CC2FC5

            payload =
            {
                'ip': '192.168.2.33',                                                                                                   // IP address
                'dn': 'NXSM200_01',                                                                                                     // Device name
                'fn': ['NXSM200_01', None, None, None, None, None, None, None],                                                         // List of friendly names
                'hn': 'NXSM200-01-4037',                                                                                                // Hostname
                'mac': '2CF432CC2FC5',                                                                                                  // MAC Adresse ohne :
                'md': 'NXSM200',                                                                                                        // Module
                'ty': 0,                                                                                                                // Tuya
                'if': 0,                                                                                                                // ifan
                'ofln': 'Offline',                                                                                                      // LWT-offline
                'onln': 'Online',                                                                                                       // LWT-online
                'state': ['OFF', 'ON', 'TOGGLE', 'HOLD'],                                                                               // StateText[0..3]
                'sw': '12.1.1',                                                                                                         // Firmware Version
                't': 'NXSM200_01',                                                                                                      // Topic
                'ft': '%prefix%/%topic%/',                                                                                              // Full Topic
                'tp': ['cmnd', 'stat', 'tele'],                                                                                         // Topic [SUB_PREFIX, PUB_PREFIX, PUB_PREFIX2]
                'rl': [1, 0, 0, 0, 0, 0, 0, 0],                                                                                         // Relays, 0: disabled, 1: relay, 2.. future extension (fan, shutter?)
                'swc': [-1, -1, -1, -1, -1, -1, -1, -1],                                                                                // SwitchMode
                'swn': [None, None, None, None, None, None, None, None],                                                                // SwitchName
                'btn': [0, 0, 0, 0, 0, 0, 0, 0],                                                                                        // Buttons
                'so': {'4': 0, '11': 0, '13': 0, '17': 0, '20': 0, '30': 0, '68': 0, '73': 0, '82': 0, '114': 0, '117': 0},             // SetOption needed by HA to map Tasmota devices to HA entities and triggers
                'lk': 0,                                                                                                                // ctrgb
                'lt_st': 0,                                                                                                             // Light subtype
                'sho': [0, 0, 0, 0],
                'sht': [[0, 0, 48], [0, 0, 46], [0, 0, 110], [0, 0, 108]],
                'ver': 1                                                                                                                // Discovery protocol version
            }
            """

            if payload['md'].lower() != 'nspanel':
                self.logger.debug(f"Discovered device with device_id={device_id} is not a NSPanel. Discovery skipped.")
                return

            tasmota_topic = payload['t']
            if tasmota_topic:
                device_name = payload['dn']
                self.logger.info(f"Discovered NSPanel with tasmota_topic={tasmota_topic} and device_name={device_name}")

                # if device is unknown, add it to dict
                if tasmota_topic not in self.tasmota_devices:
                    self.logger.info(f"New NSPanel based on Discovery Message found.")
                    self._add_new_device_to_tasmota_devices(tasmota_topic)

                # process decoding message and set device to status 'discovered'
                self.tasmota_devices[tasmota_topic]['ip'] = payload['ip']
                self.tasmota_devices[tasmota_topic]['friendly_name'] = payload['fn'][0]
                self.tasmota_devices[tasmota_topic]['fw_ver'] = payload['sw']
                self.tasmota_devices[tasmota_topic]['device_id'] = device_id
                self.tasmota_devices[tasmota_topic]['module'] = payload['md']
                self.tasmota_devices[tasmota_topic]['mac'] = ':'.join(device_id[i:i + 2] for i in range(0, 12, 2))
                self.tasmota_devices[tasmota_topic]['discovery_config'] = self._rename_discovery_keys(payload)
                self.tasmota_devices[tasmota_topic]['status'] = 'discovered'

                # start device interview
                self._interview_device(tasmota_topic)

                if payload['ft'] != self.full_topic:
                    self.logger.warning(f"Device {device_name} discovered, but FullTopic of device does not match plugin setting!")

        elif msg_type == 'sensors':
            """
            device_id = 2CF432CC2FC5

            payload = {'sn': {'Time': '2022-11-19T13:35:59',
                              'ENERGY': {'TotalStartTime': '2019-12-23T17:02:03', 'Total': 85.314, 'Yesterday': 0.0,
                                         'Today': 0.0, 'Power': 0, 'ApparentPower': 0, 'ReactivePower': 0, 'Factor': 0.0,
                                         'Voltage': 0, 'Current': 0.0}}, 'ver': 1}
                                         
            payload = {"sn": {"Time":"2022-11-28T16:17:32",
                              "ANALOG":{"Temperature1":23.2},
                              "ESP32":{"Temperature":28.9},
                              "TempUnit":"C"},
                      "ver":1}
            """

            # get payload with Sensor information
            sensor_payload = payload['sn']
            if 'Time' in sensor_payload:
                sensor_payload.pop('Time')

            self.logger.debug(f"on_mqtt_discovery_message - sensor: {device_id=}, {sensor_payload=}, {self.tasmota_devices=}")

            # ToDo: Lösen von "dictionary changed size during iteration" und Übertrag in Tasmota Plugin
            """
            # ToDo: Übertrag in Tasmota Plugin (dictionary changed size during iteration)
            # find matching tasmota_topic
            tasmota_topic = None
            for entry in list(self.tasmota_devices.keys()):
                if self.tasmota_devices[entry].get('device_id') == device_id:
                    tasmota_topic = entry
                    break

            # hand over sensor information payload for parsing
            if sensor_payload and tasmota_topic:
                self.logger.info(f"Discovered Tasmota Device with topic={tasmota_topic} and SensorInformation")
                self._handle_sensor(tasmota_topic, '', sensor_payload)
            """

    def on_mqtt_lwt_message(self, topic: str, payload: bool, qos: int = None, retain: bool = None) -> None:
        """
        Callback function to handle received lwt messages

        :param topic:       MQTT topic
        :param payload:     MQTT message payload
        :param qos:         qos for this message (optional)
        :param retain:      retain flag for this message (optional)

        """

        try:
            (topic_type, tasmota_topic, info_topic) = topic.split('/')
        except Exception as e:
            self.logger.error(f"received topic {topic} is not in correct format. Error was: {e}")
        else:
            self.logger.info(f"Received LWT Message for {tasmota_topic} with value={payload} and retain={retain}")

            if payload:
                if tasmota_topic not in self.tasmota_devices:
                    self.logger.debug(f"New online device based on LWT Message discovered.")
                    self._handle_new_discovered_device(tasmota_topic)
                self.tasmota_devices[tasmota_topic]['online_timeout'] = datetime.now() + timedelta(seconds=self.telemetry_period + 5)

            if tasmota_topic in self.tasmota_devices:
                self.tasmota_devices[tasmota_topic]['online'] = payload
                self._set_item_value(tasmota_topic, 'item_online', payload, info_topic)

    def on_mqtt_status0_message(self, topic: str, payload: dict, qos: int = None, retain: bool = None) -> None:
        """
        Callback function to handle received messages

        :param topic:       MQTT topic
        :param payload:     MQTT message payload
        :param qos:         qos for this message
        :param retain:      retain flag for this message

        """

        """ 
        Example payload 

        payload = {'Status': {'Module': 75, 'DeviceName': 'ZIGBEE_Bridge01', 'FriendlyName': ['SONOFF_ZB1'],
                              'Topic': 'SONOFF_ZB1', 'ButtonTopic': '0', 'Power': 0, 'PowerOnState': 3, 'LedState': 1,
                              'LedMask': 'FFFF', 'SaveData': 1, 'SaveState': 1, 'SwitchTopic': '0',
                              'SwitchMode': [0, 0, 0, 0, 0, 0, 0, 0], 'ButtonRetain': 0, 'SwitchRetain': 0,
                              'SensorRetain': 0, 'PowerRetain': 0, 'InfoRetain': 0, 'StateRetain': 0},
                   'StatusPRM': {'Baudrate': 115200, 'SerialConfig': '8N1', 'GroupTopic': 'tasmotas',
                                 'OtaUrl': 'http://ota.tasmota.com/tasmota/release/tasmota-zbbridge.bin.gz',
                                 'RestartReason': 'Software/System restart', 'Uptime': '0T23:18:30',
                                 'StartupUTC': '2022-11-19T12:10:15', 'Sleep': 50, 'CfgHolder': 4617, 'BootCount': 116,
                                 'BCResetTime': '2021-04-28T08:32:10', 'SaveCount': 160, 'SaveAddress': '1FB000'},
                   'StatusFWR': {'Version': '12.1.1(zbbridge)', 'BuildDateTime': '2022-08-25T11:37:17', 'Boot': 31,
                                 'Core': '2_7_4_9', 'SDK': '2.2.2-dev(38a443e)', 'CpuFrequency': 160,
                                 'Hardware': 'ESP8266EX', 'CR': '372/699'},
                   'StatusLOG': {'SerialLog': 0, 'WebLog': 2, 'MqttLog': 0, 'SysLog': 0, 'LogHost': '', 'LogPort': 514,
                                 'SSId': ['WLAN-Access', ''], 'TelePeriod': 300, 'Resolution': '558180C0',
                                 'SetOption': ['00008009', '2805C80001000600003C5A0A002800000000', '00000080',
                                               '40046002', '00004810', '00000000']},
                   'StatusMEM': {'ProgramSize': 685, 'Free': 1104, 'Heap': 25, 'ProgramFlashSize': 2048,
                                 'FlashSize': 2048, 'FlashChipId': '1540A1', 'FlashFrequency': 40, 'FlashMode': 3,
                                 'Features': ['00000809', '0F1007C6', '04400001', '00000003', '00000000', '00000000',
                                              '00020080', '00200000', '04000000', '00000000'],
                                 'Drivers': '1,2,4,7,9,10,12,20,23,38,41,50,62', 'Sensors': '1'},
                   'StatusNET': {'Hostname': 'SONOFF-ZB1-6926', 'IPAddress': '192.168.2.24', 'Gateway': '192.168.2.1',
                                 'Subnetmask': '255.255.255.0', 'DNSServer1': '192.168.2.1', 'DNSServer2': '0.0.0.0',
                                 'Mac': '84:CC:A8:AA:1B:0E', 'Webserver': 2, 'HTTP_API': 1, 'WifiConfig': 0,
                                 'WifiPower': 17.0},
                   'StatusMQT': {'MqttHost': '192.168.2.12', 'MqttPort': 1883, 'MqttClientMask': 'DVES_%06X',
                                 'MqttClient': 'DVES_AA1B0E', 'MqttUser': 'DVES_USER', 'MqttCount': 1,
                                 'MAX_PACKET_SIZE': 1200, 'KEEPALIVE': 30, 'SOCKET_TIMEOUT': 4},
                   'StatusTIM': {'UTC': '2022-11-20T11:28:45', 'Local': '2022-11-20T12:28:45',
                                 'StartDST': '2022-03-27T02:00:00', 'EndDST': '2022-10-30T03:00:00',
                                 'Timezone': '+01:00', 'Sunrise': '08:07', 'Sunset': '17:04'},
                   'StatusSNS': {'Time': '2022-11-20T12:28:45'},
                   'StatusSTS': {'Time': '2022-11-20T12:28:45', 'Uptime': '0T23:18:30', 'UptimeSec': 83910, 'Vcc': 3.41,
                                 'Heap': 24, 'SleepMode': 'Dynamic', 'Sleep': 50, 'LoadAvg': 19, 'MqttCount': 1,
                                 'Wifi': {'AP': 1, 'SSId': 'WLAN-Access', 'BSSId': '38:10:D5:15:87:69', 'Channel': 1,
                                          'Mode': '11n', 'RSSI': 50, 'Signal': -75, 'LinkCount': 1,
                                          'Downtime': '0T00:00:03'}}}

        """

        try:
            (topic_type, tasmota_topic, info_topic) = topic.split('/')
            self.logger.info(
                f"on_mqtt_status0_message: topic_type={topic_type}, tasmota_topic={tasmota_topic}, info_topic={info_topic}, payload={payload}")
        except Exception as e:
            self.logger.error(f"received topic {topic} is not in correct format. Error was: {e}")

        else:
            self.logger.info(f"Received Status0 Message for {tasmota_topic} with value={payload} and retain={retain}")
            self.tasmota_devices[tasmota_topic]['status'] = 'interviewed'

            # handle teleperiod
            self._handle_teleperiod(tasmota_topic, payload['StatusLOG'])

            if self.tasmota_devices[tasmota_topic]['status'] != 'interviewed':
                if self.tasmota_devices[tasmota_topic]['status'] != 'discovered':
                    # friendly name
                    self.tasmota_devices[tasmota_topic]['friendly_name'] = payload['Status']['FriendlyName'][0]

                    # IP Address
                    ip = payload['StatusNET']['IPAddress']
                    ip_eth = payload['StatusNET'].get('Ethernet', {}).get('IPAddress')
                    ip = ip_eth if ip == '0.0.0.0' else None
                    self.tasmota_devices[tasmota_topic]['ip'] = ip

                    # Firmware
                    self.tasmota_devices[tasmota_topic]['fw_ver'] = payload['StatusFWR']['Version'].split('(')[0]

                    # MAC
                    self.tasmota_devices[tasmota_topic]['mac'] = payload['StatusNET']['Mac']

                # Module No
                self.tasmota_devices[tasmota_topic]['template'] = payload['Status']['Module']

            # get detailed status using payload['StatusSTS']
            status_sts = payload['StatusSTS']

            # Handling Lights and Dimmer
            if any([i in status_sts for i in self.LIGHT_MSG]):
                self._handle_lights(tasmota_topic, info_topic, status_sts)

            # Handling of Power
            if any(item.startswith("POWER") for item in status_sts.keys()):
                self._handle_power(tasmota_topic, info_topic, status_sts)

            # Handling of RF messages
            if any(item.startswith("Rf") for item in status_sts.keys()):
                self._handle_rf(tasmota_topic, info_topic, status_sts)

            # Handling of Wi-Fi
            if 'Wifi' in status_sts:
                self._handle_wifi(tasmota_topic, status_sts['Wifi'])

            # Handling of Uptime
            if 'Uptime' in status_sts:
                self._handle_uptime(tasmota_topic, status_sts['Uptime'])

            # Handling of UptimeSec
            if 'UptimeSec' in status_sts:
                self.logger.info(f"Received Message contains UptimeSec information.")
                self._handle_uptime_sec(tasmota_topic, status_sts['UptimeSec'])

    def on_mqtt_info_message(self, topic: str, payload: dict, qos: int = None, retain: bool = None) -> None:
        """
        Callback function to handle received messages

        :param topic:       MQTT topic
        :param payload:     MQTT message payload
        :param qos:         qos for this message (optional)
        :param retain:      retain flag for this message (optional)

        """

        try:
            (topic_type, tasmota_topic, info_topic) = topic.split('/')
            self.logger.info(
                f"on_mqtt_message: topic_type={topic_type}, tasmota_topic={tasmota_topic}, info_topic={info_topic}, payload={payload}")
        except Exception as e:
            self.logger.error(f"received topic {topic} is not in correct format. Error was: {e}")
        else:
            if info_topic == 'INFO1':
                # payload={'Info1': {'Module': 'Sonoff Basic', 'Version': '11.0.0(tasmota)', 'FallbackTopic': 'cmnd/DVES_2EB8AE_fb/', 'GroupTopic': 'cmnd/tasmotas/'}}
                self.logger.info(f"Received Message decoded as INFO1 message.")
                self.tasmota_devices[tasmota_topic]['fw_ver'] = payload['Info1']['Version'].split('(')[0]
                self.tasmota_devices[tasmota_topic]['module_no'] = payload['Info1']['Module']

            elif info_topic == 'INFO2':
                # payload={'Info2': {'WebServerMode': 'Admin', 'Hostname': 'SONOFF-B1-6318', 'IPAddress': '192.168.2.25'}}
                self.logger.info(f"Received Message decoded as INFO2 message.")
                self.tasmota_devices[tasmota_topic]['ip'] = payload['Info2']['IPAddress']

            elif info_topic == 'INFO3':
                # payload={'Info3': {'RestartReason': 'Software/System restart', 'BootCount': 1395}}
                self.logger.info(f"Received Message decoded as INFO3 message.")
                restart_reason = payload['Info3']['RestartReason']
                self.logger.warning(f"Device {tasmota_topic} (IP={self.tasmota_devices[tasmota_topic]['ip']}) just startet. Reason={restart_reason}")

    def on_mqtt_message(self, topic: str, payload: dict, qos: int = None, retain: bool = None) -> None:
        """
        Callback function to handle received messages

        :param topic:       MQTT topic
        :param payload:     MQTT message payload
        :param qos:         qos for this message (optional)
        :param retain:      retain flag for this message (optional)

        """

        # tele/NSPanel1/STATE = {"Time":"2022-12-03T13:16:26","Uptime":"0T00:25:13","UptimeSec":1513,"Heap":127,"SleepMode":"Dynamic","Sleep":0,"LoadAvg":999,"MqttCount":1,"Berry":{"HeapUsed":14,"Objects":218},"POWER1":"ON","POWER2":"OFF","Wifi":{"AP":1,"SSId":"FritzBox","BSSId":"F0:B0:14:4A:08:CD","Channel":1,"Mode":"11n","RSSI":42,"Signal":-79,"LinkCount":1,"Downtime":"0T00:00:07"}}
        # tele/NSPanel1/SENSOR = {"Time":"2022-12-03T13:16:26","ANALOG":{"Temperature1":27.4},"ESP32":{"Temperature":37.8},"TempUnit":"C"}
        #: topic_type = tele, tasmota_topic = NSPanel1, info_topic = RESULT, payload = {'CustomRecv': 'event,startup,45,eu'}
        try:
            (topic_type, tasmota_topic, info_topic) = topic.split('/')
            self.logger.info(f"on_mqtt_message: topic_type={topic_type}, tasmota_topic={tasmota_topic}, info_topic={info_topic}, payload={payload}")
        except Exception as e:
            self.logger.error(f"received topic {topic} is not in correct format. Error was: {e}")
        else:

            # handle unknown device
            if tasmota_topic not in self.tasmota_devices:
                self._handle_new_discovered_device(tasmota_topic)

            # handle message
            if isinstance(payload, dict) and info_topic in ['STATE', 'RESULT']:

                # Handling of TelePeriod
                if 'TelePeriod' in payload:
                    self.logger.info(f"Received Message decoded as teleperiod message.")
                    self._handle_teleperiod(tasmota_topic, payload['TelePeriod'])

                elif 'Module' in payload:
                    self.logger.info(f"Received Message decoded as Module message.")
                    self._handle_module(tasmota_topic, payload['Module'])

                # Handling of Light messages
                elif 'CustomRecv' in payload:
                    self.logger.info(f"Received Message decoded as NSPanel Message, will be put to queue for logging reasons. {self.custom_msg_queue.qsize() + 1} messages logged.")
                    self.custom_msg_queue.put(payload['CustomRecv'])
                    self.HandlePanelMessage(tasmota_topic, info_topic, payload['CustomRecv'])

                # Handling of Power messages
                elif any(item.startswith("POWER") for item in payload.keys()):
                    self.logger.info(f"Received Message decoded as power message.")
                    self._handle_power(tasmota_topic, info_topic, payload)

                # Handling of Setting messages
                elif next(iter(payload)).startswith("SetOption"):
                    # elif any(item.startswith("SetOption") for item in payload.keys()):
                    self.logger.info(f"Received Message decoded as Tasmota Setting message.")
                    self._handle_setting(tasmota_topic, payload)

                # Handling of Wi-Fi
                if 'Wifi' in payload:
                    self.logger.info(f"Received Message contains Wifi information.")
                    self._handle_wifi(tasmota_topic, payload['Wifi'])

                # Handling of Uptime
                if 'Uptime' in payload:
                    self.logger.info(f"Received Message contains Uptime information.")
                    self._handle_uptime(tasmota_topic, payload['Uptime'])

                # Handling of UptimeSec
                if 'UptimeSec' in payload:
                    self.logger.info(f"Received Message contains UptimeSec information.")
                    self._handle_uptime_sec(tasmota_topic, payload['UptimeSec'])

            elif isinstance(payload, dict) and info_topic == 'SENSOR':
                self.logger.info(f"Received Message contains sensor information.")
                self._handle_sensor(tasmota_topic, info_topic, payload)

            else:
                self.logger.warning(f"Received Message '{payload}' not handled within plugin.")

            # setting new online-timeout
            self.tasmota_devices[tasmota_topic]['online_timeout'] = datetime.now() + timedelta(seconds=self.telemetry_period + 5)

            # setting online_item to True
            self._set_item_value(tasmota_topic, 'item_online', True, info_topic)

    def on_mqtt_power_message(self, topic: str, payload: dict, qos: int = None, retain: bool = None) -> None:
        """
        Callback function to handle received messages

        :param topic:       MQTT topic
        :param payload:     MQTT message payload
        :param qos:         qos for this message (optional)
        :param retain:      retain flag for this message (optional)

        """

        # check for retained message and handle it
        if bool(retain):
            if topic not in self.topics_of_retained_messages:
                self.topics_of_retained_messages.append(topic)
        else:
            if topic in self.topics_of_retained_messages:
                self.topics_of_retained_messages.remove(topic)

        # handle incoming message
        try:
            (topic_type, tasmota_topic, info_topic) = topic.split('/')
            self.logger.info(
                f"on_mqtt_power_message: topic_type={topic_type}, tasmota_topic={tasmota_topic}, info_topic={info_topic}, payload={payload}")
        except Exception as e:
            self.logger.error(f"received topic {topic} is not in correct format. Error was: {e}")
        else:
            device = self.tasmota_devices.get(tasmota_topic, None)
            if device:
                if info_topic.startswith('POWER'):
                    tasmota_relay = str(info_topic[5:])
                    tasmota_relay = '1' if not tasmota_relay else None
                    item_relay = f'item_relay{tasmota_relay}'
                    self._set_item_value(tasmota_topic, item_relay, payload == 'ON', info_topic)
                    self.tasmota_devices[tasmota_topic]['relais'][info_topic] = payload

    ################################
    # MQTT Stuff
    ################################

    def publish_tasmota_topic(self, prefix: str = 'cmd', topic: str = None, detail: str = None, payload: str = None, item=None, qos: int = None, retain: bool = False, bool_values: list = None) -> None:
        """
        build the topic in Tasmota style and publish to mqtt

        :param prefix:          prefix of topic to publish
        :param topic:           unique part of topic to publish
        :param detail:          detail of topic to publish
        :param payload:         payload to publish
        :param item:            item (if relevant)
        :param qos:             qos for this message (optional)
        :param retain:          retain flag for this message (optional)
        :param bool_values:     bool values (for publishing this topic, optional)

        """

        topic = topic if topic is not None else self.tasmota_topic
        detail = detail if detail is not None else 'CustomSend'

        tpc = self.full_topic.replace("%prefix%", prefix)
        tpc = tpc.replace("%topic%", topic)
        tpc += detail

        # self.logger.debug(f"publish_topic with {tpc=}, {payload=}")
        self.publish_topic(tpc, payload, item, qos, retain, bool_values)

    def add_tasmota_subscription(self, prefix: str, topic: str, detail: str, payload_type: str, bool_values: list = None, item=None, callback=None) -> None:
        """
        build the topic in Tasmota style and add the subscription to mqtt

        :param prefix:          prefix of topic to subscribe to
        :param topic:           unique part of topic to subscribe to
        :param detail:          detail of topic to subscribe to
        :param payload_type:    payload type of the topic (for this subscription to the topic)
        :param bool_values:     bool values (for this subscription to the topic)
        :param item:            item that should receive the payload as value. Used by the standard handler (if no callback function is specified)
        :param callback:        a plugin can provide an own callback function, if special handling of the payload is needed

        """

        tpc = self.full_topic.replace("%prefix%", prefix)
        tpc = tpc.replace("%topic%", topic)
        tpc += detail
        self.add_subscription(tpc, payload_type, bool_values=bool_values, callback=callback)

    ################################
    # Tasmota Stuff
    ################################

    def _set_item_value(self, tasmota_topic: str, itemtype: str, value, info_topic: str = '') -> None:
        """
        Sets item value

        :param tasmota_topic:   MQTT message payload
        :param itemtype:        itemtype to be set
        :param value:           value to be set
        :param info_topic:      MQTT info_topic
        """

        if tasmota_topic in self.tasmota_devices:

            # create source of item value
            src = f"{tasmota_topic}:{info_topic}" if info_topic != '' else f"{tasmota_topic}"

            if itemtype in self.tasmota_devices[tasmota_topic]['connected_items']:
                # get item to be set
                item = self.tasmota_devices[tasmota_topic]['connected_items'][itemtype]

                # set item value
                self.logger.info(f"{tasmota_topic}: Item '{item.id()}' via itemtype '{itemtype}' set to value '{value}' provided by '{src}'.")
                item(value, self.get_shortname(), src)

            else:
                self.logger.debug(f"{tasmota_topic}: No item for itemtype '{itemtype}' defined to set to '{value}' provided by '{src}'.")
        else:
            self.logger.debug(f"{tasmota_topic} unknown.")

    def _handle_new_discovered_device(self, tasmota_topic):
        self.logger.debug(f"_handle_new_discovered_device called with {tasmota_topic=}")

        self._add_new_device_to_tasmota_devices(tasmota_topic)
        self.tasmota_devices[tasmota_topic]['status'] = 'discovered'
        # self._interview_device(tasmota_topic)

    def _add_new_device_to_tasmota_devices(self, tasmota_topic):
        self.logger.debug(f"_add_new_device_to_tasmota_devices called with {tasmota_topic=}")

        self.tasmota_devices[tasmota_topic] = {}
        self.tasmota_devices[tasmota_topic]['connected_to_item'] = False
        self.tasmota_devices[tasmota_topic]['online'] = False
        self.tasmota_devices[tasmota_topic]['status'] = 'None'
        self.tasmota_devices[tasmota_topic]['connected_items'] = {}
        self.tasmota_devices[tasmota_topic]['uptime'] = '-'
        self.tasmota_devices[tasmota_topic]['sensors'] = {}
        self.tasmota_devices[tasmota_topic]['relais'] = {}

        self.logger.debug(f"_add_new_device_to_tasmota_devices done")

    def _set_device_offline(self, tasmota_topic):

        self.tasmota_devices[tasmota_topic]['online'] = False
        self._set_item_value(tasmota_topic, 'item_online', False, 'check_online_status')
        self.logger.info(f"{tasmota_topic} is not online any more - online_timeout={self.tasmota_devices[tasmota_topic]['online_timeout']}, now={datetime.now()}")

        # clean data from dict to show correct status
        self.tasmota_devices[tasmota_topic]['lights'] = {}
        self.tasmota_devices[tasmota_topic]['rf'] = {}
        self.tasmota_devices[tasmota_topic]['sensors'] = {}
        self.tasmota_devices[tasmota_topic]['relais'] = {}
        self.tasmota_devices[tasmota_topic]['zigbee'] = {}

    def _check_online_status(self):
        """
        checks all tasmota topics, if last message is with telemetry period. If not set tasmota_topic offline

        """

        self.logger.info("_check_online_status: Checking online status of connected devices")
        for tasmota_topic in self.tasmota_devices:
            if self.tasmota_devices[tasmota_topic].get('online') is True and self.tasmota_devices[tasmota_topic].get('online_timeout'):
                if self.tasmota_devices[tasmota_topic]['online_timeout'] < datetime.now():
                    self._set_device_offline(tasmota_topic)
                else:
                    self.logger.debug(f'_check_online_status: Checking online status of {tasmota_topic} successful')

    def _interview_device(self, topic: str) -> None:
        """
        ask for status info of each known tasmota_topic

        :param topic:          tasmota Topic
        """

        self.logger.debug(f"_interview_device called with {topic=}")

        # self.logger.debug(f"run: publishing 'cmnd/{topic}/Status0'")
        self.publish_tasmota_topic(prefix='cmnd', detail='Status0', payload='')

        # self.logger.debug(f"run: publishing 'cmnd/{topic}/State'")
        # self.publish_tasmota_topic('cmnd', topic, 'State', '')

        # self.logger.debug(f"run: publishing 'cmnd/{topic}/Module'")
        # self.publish_tasmota_topic('cmnd', topic, 'Module', '')

    def _set_telemetry_period(self, topic: str) -> None:
        """
        sets telemetry period for given topic/device

        :param topic:          tasmota Topic
        """

        self.logger.info(f"run: Setting telemetry period to {self.telemetry_period} seconds")
        self.publish_tasmota_topic('cmnd', topic, 'teleperiod', self.telemetry_period)

    def _handle_wifi(self, device: str, payload: dict) -> None:
        """
        Extracts Wi-Fi information out of payload and updates plugin dict

        :param device:          Device, the Zigbee Status information shall be handled
        :param payload:         MQTT message payload

        """
        self.logger.debug(f"_handle_wifi: received payload={payload}")
        wifi_signal = payload.get('Signal')
        if wifi_signal:
            if isinstance(wifi_signal, str) and wifi_signal.isdigit():
                wifi_signal = int(wifi_signal)
            self.tasmota_devices[device]['wifi_signal'] = wifi_signal

    def _handle_setting(self, device: str, payload: dict) -> None:
        """
        Extracts Zigbee Bridge Setting information out of payload and updates dict
        :param device:
        :param payload:     MQTT message payload
        """

        # handle Setting listed in Zigbee Bridge Settings (wenn erster Key des Payload-Dict in Zigbee_Bridge_Default_Setting...)
        if next(iter(payload)) in self.ZIGBEE_BRIDGE_DEFAULT_OPTIONS:
            if not self.tasmota_devices[device]['zigbee'].get('setting'):
                self.tasmota_devices[device]['zigbee']['setting'] = {}
            self.tasmota_devices[device]['zigbee']['setting'].update(payload)

            if self.tasmota_devices[device]['zigbee']['setting'] == self.ZIGBEE_BRIDGE_DEFAULT_OPTIONS:
                self.tasmota_devices[device]['zigbee']['status'] = 'set'
                self.logger.info(f'_handle_setting: Setting of Tasmota Zigbee Bridge successful.')

    def _handle_teleperiod(self, tasmota_topic: str, teleperiod: dict) -> None:

        self.tasmota_devices[tasmota_topic]['teleperiod'] = teleperiod
        if teleperiod != self.telemetry_period:
            self._set_telemetry_period(tasmota_topic)

    def _handle_uptime(self, tasmota_topic: str, uptime: str) -> None:
        self.logger.debug(f"Received Message contains Uptime information. uptime={uptime}")
        self.tasmota_devices[tasmota_topic]['uptime'] = uptime

    def _handle_uptime_sec(self, tasmota_topic: str, uptime_sec: int) -> None:
        self.logger.debug(f"Received Message contains UptimeSec information. uptime={uptime_sec}")
        self.tasmota_devices[tasmota_topic]['UptimeSec'] = int(uptime_sec)

    def _handle_power(self, device: str, function: str, payload: dict) -> None:
        """
        Extracts Power information out of payload and updates plugin dict

        :param device:          Device, the Power information shall be handled (equals tasmota_topic)
        :param function:        Function of Device (equals info_topic)
        :param payload:         MQTT message payload

        """
        # payload = {"Time": "2022-11-21T12:56:34", "Uptime": "0T00:00:11", "UptimeSec": 11, "Heap": 27, "SleepMode": "Dynamic", "Sleep": 50, "LoadAvg": 19, "MqttCount": 0, "POWER1": "OFF", "POWER2": "OFF", "POWER3": "OFF", "POWER4": "OFF", "Wifi": {"AP": 1, "SSId": "WLAN-Access", "BSSId": "38:10:D5:15:87:69", "Channel": 1, "Mode": "11n", "RSSI": 82, "Signal": -59, "LinkCount": 1, "Downtime": "0T00:00:03"}}

        power_dict = {key: val for key, val in payload.items() if key.startswith('POWER')}
        self.tasmota_devices[device]['relais'].update(power_dict)
        for power in power_dict:
            relay_index = 1 if len(power) == 5 else str(power[5:])
            item_relay = f'item_relay{relay_index}'
            self._set_item_value(device, item_relay, power_dict[power], function)

    def _handle_sensor(self, device: str, function: str, payload: dict) -> None:
        """

        :param device:
        :param function:
        :param payload:
        :return:
        """

        # tele / NSPanel1 / SENSOR = {"Time": "2022-12-03T13:21:26", "ANALOG": {"Temperature1": 28.0}, "ESP32": {"Temperature": 38.9}, "TempUnit": "C"}
        # Handling of Zigbee Device Messages

        for sensor in self.TEMP_SENSOR:
            data = payload.get(sensor)

            if data and isinstance(data, dict):
                self.logger.info(f"Received Message decoded as {sensor} Sensor message.")
                if sensor not in self.tasmota_devices[device]['sensors']:
                    self.tasmota_devices[device]['sensors'][sensor] = {}

                for key in self.TEMP_SENSOR_KEYS:
                    if key in data:
                        self.tasmota_devices[device]['sensors'][sensor][key.lower()] = data[key]
                        self._set_item_value(device, self.TEMP_SENSOR_KEYS[key], data[key], function)

    def _rename_discovery_keys(self, payload: dict) -> dict:

        link = {'ip':    'IP',
                'dn':    'DeviceName',
                'fn':    'FriendlyNames',  # list
                'hn':    'HostName',
                'mac':   'MAC',
                'md':    'Module',
                'ty':    'Tuya',
                'if':    'ifan',
                'ofln':  'LWT-offline',
                'onln':  'LWT-online',
                'state': 'StateText',  # [0..3]
                'sw':    'FirmwareVersion',
                't':     'Topic',
                'ft':    'FullTopic',
                'tp':    'Prefix',
                'rl':    'Relays',    # 0: disabled, 1: relay, 2.. future extension (fan, shutter?)
                'swc':   'SwitchMode',
                'swn':   'SwitchName',
                'btn':   'Buttons',
                'so':    'SetOption',  # needed by HA to map Tasmota devices to HA entities and triggers
                'lk':    'ctrgb',
                'lt_st': 'LightSubtype',
                'sho':   'sho',
                'sht':   'sht',
                'ver':   'ProtocolVersion',
                }

        new_payload = {}
        for k_old in payload:
            k_new = link.get(k_old)
            if k_new:
                new_payload[k_new] = payload[k_old]

        return new_payload

    ################################
    #  NSPage Stuff
    ################################

    def _parse_config_file(self):
        """
        Parse the page config file and check for completeness
        """

        with open(self.config_file_location, 'r') as stream:
            try:
                config = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                self.logger.warning(f"Exception during parsing of  page config yaml file occurred: {exc}")
                return None

        # ToDo: Für alle Pages die Anzahl der Entities prüfen
        # ToDo: Für alle Pages die Konfig auf Vollständigkeit prüfen

        self.logger.debug(f"_parse_config_file: page-config={config} available!")
        return config

    def _get_items_of_panel_config_to_update_item(self):
        """
        Put all item out ouf config file to update_item
        """

        for card in self.panel_config['cards']:
            entities = card.get('entities')
            if entities is not None:
                for entity in entities:
                    item = self._get_item(entity.get('internalNameEntity'))
                    if item is not None:
                        self.logger.debug(f"Item={item} will be watched for updates")
                        return self.update_item

    def _next_page(self):
        """
        return next_page number
        """

        self.current_page += 1
        if self.current_page >= len(self.panel_config['cards']):
            self.current_page -= len(self.panel_config['cards'])
        self.logger.debug(f"next_page={self.current_page}")
        return self.current_page

    def _previous_page(self):
        """
        return previous_page number
        """

        self.current_page -= 1
        if self.current_page < 0:
            self.current_page += len(self.panel_config['cards'])
        self.logger.debug(f"previous_page={self.current_page}")
        return self.current_page

    def _get_item(self, internalNameEntity):
        """
        get item from item path
        """

        self.logger.debug(f"_get_item: look for item for internalNameEntity={internalNameEntity}")
        item = items.return_item(internalNameEntity)
        if item is not None:
            self.logger.debug(f"item={item} for internalNameEntity={internalNameEntity} identified")
        else:
            self.logger.debug(f"No corresponding item for internalNameEntity={internalNameEntity} identified")
        return item

    def send_current_time(self):
        self.publish_tasmota_topic(payload=f"time~{time.strftime('%H:%M', time.localtime())}")

    def send_current_date(self):
        self.publish_tasmota_topic(payload=f"date~{time.strftime('%A, %d.%B %Y', time.localtime())}")

    def send_screensavertimeout(self):
        screensavertimeout = self.panel_config.get('config', {}).get('screensaver_timeout', 10)
        self.publish_tasmota_topic(payload=f"timeout~{screensavertimeout}")

    def send_panel_brightness(self):
        brightness_screensaver = self.panel_config.get('config', {}).get('brightness_screensaver', 10)
        brightness_active = self.panel_config.get('config', {}).get('brightness_active', 100)
        self.publish_tasmota_topic(payload=f"dimmode~{brightness_screensaver}~{brightness_active}~6371")

    def HandlePanelMessage(self, device: str, function: str, payload: str) -> None:
        """
        # general
        event,buttonPress2,pageName,bNext
        event,buttonPress2,pageName,bPrev
        event,buttonPress2,pageName,bExit,number_of_taps
        event,buttonPress2,pageName,sleepReached
        # startup page
        event,startup,version,model
        # screensaver page
        event,buttonPress2,screensaver,exit - Touch Event on Screensaver
        event,screensaverOpen - Screensaver has opened
        # cardEntities Page
        event,*eventName*,*entityName*,*actionName*,*optionalValue*
        event,buttonPress2,internalNameEntity,up
        event,buttonPress2,internalNameEntity,down
        event,buttonPress2,internalNameEntity,stop
        event,buttonPress2,internalNameEntity,OnOff,1
        event,buttonPress2,internalNameEntity,button
        # popupLight Page
        event,pageOpenDetail,popupLight,internalNameEntity
        event,buttonPress2,internalNameEntity,OnOff,1
        event,buttonPress2,internalNameEntity,brightnessSlider,50
        event,buttonPress2,internalNameEntity,colorTempSlider,50
        event,buttonPress2,internalNameEntity,colorWheel,x|y|wh
        # popupShutter Page
        event,pageOpenDetail,popupShutter,internalNameEntity
        event,buttonPress2,internalNameEntity,positionSlider,50
        # popupNotify Page
        event,buttonPress2,*internalName*,notifyAction,yes
        event,buttonPress2,*internalName*,notifyAction,no
        # cardThermo Page
        event,buttonPress2,*entityName*,tempUpd,*temperature*
        event,buttonPress2,*entityName*,hvac_action,*hvac_action*
        # cardMedia Page
        event,buttonPress2,internalNameEntity,media-back
        event,buttonPress2,internalNameEntity,media-pause
        event,buttonPress2,internalNameEntity,media-next
        event,buttonPress2,internalNameEntity,volumeSlider,75
        # cardAlarm Page
        event,buttonPress2,internalNameEntity,actionName,code
        """

        try:
            content_list = payload.split(',')
        except Exception as e:
            self.logger.warning(f"During handling of payload exception occurred: {e}")
            return None

        self.logger.debug(f"HandlePanelMessage: content_list={content_list}, length of content_list={len(content_list)}")

        typ = content_list[0]
        method = content_list[1]
        page = content_list[2]
        words = content_list

        self.logger.debug(f"HandlePanelMessage called with: typ={typ}, method={method}, page={page}, words={words}")

        if typ == 'event':
            if method == 'startup':
                self.screensaverEnabled = False
                self.panel_version = words[2]
                self.panel_model = words[3]
                self.HandleStartupProcess()
                self.current_page = 0
                self.HandleScreensaver()

            elif method == 'sleepReached':
                # event,sleepReached,cardEntities
                self.useMediaEvents = False
                self.screensaverEnabled = True
                self.current_page = 0
                self.HandleScreensaver()

            elif method == 'pageOpenDetail':
                # event,pageOpenDetail,popupLight,internalNameEntity
                self.screensaverEnabled = False
                pageItem = self.findPageItem(words[3])
                if pageItem is not None:
                    self.SendToPanel(self.GenerateDetailPage(words[2], pageItem))

            elif method == 'buttonPress2':
                self.screensaverEnabled = False
                self.logger.debug(f"{words[0]} - {words[1]} - {words[2]} - {words[3]} - {words[4]}")
                self.HandleButtonEvent(words)

            elif method == 'button1':
                self.screensaverEnabled = False
                # HandleHardwareButton(method)

            elif method == 'button2':
                self.screensaverEnabled = False
                # HandleHardwareButton(method)

    def HandleStartupProcess(self):
        self.logger.debug("HandleStartupProcess called")
        self.send_current_time()
        self.send_current_date()
        self.send_screensavertimeout()
        self.send_panel_brightness()

    def HandleScreensaver(self):
        self.publish_tasmota_topic(payload="pageType~screensaver")
        self.HandleScreensaverUpdate()
        self.HandleScreensaverColors()

    def HandleScreensaverUpdate(self):
        self.logger.debug('Function HandleScreensaverUpdate to be done')
        heading = self.panel_config.get('screensaver', {}).get('heading', '')
        text = self.panel_config.get('screensaver', {}).get('text', '')
        self.publish_tasmota_topic(payload=f"notify~{heading}~{text}")

    def HandleScreensaverColors(self):
        self.logger.info('Function HandleScreensaverColors to be done')

    def HandleButtonEvent(self, words):

        pageName = words[2]
        buttonAction = words[3]

        self.logger.debug(f"{words[0]} - {words[1]} - {words[2]} - {words[3]} - {words[4]} - {self.current_page=}")

        if 'navigate' in pageName:
            self.GeneratePage(pageName[8:len(pageName)])

        if buttonAction == 'bNext':
            self.GeneratePage(self.panel_config['cards'][self._next_page()])

        elif buttonAction == 'bPrev':
            self.GeneratePage(self.panel_config['cards'][self._previous_page()])

        elif buttonAction == 'bExit':
            self.GeneratePage(self.panel_config['cards'][0])

    def findPageItem(self, searching: str):
        activePage = self.panel_config['cards'][self.current_page]
        pageItem = next((item for item in activePage if item["entity"] == searching), None)

        return pageItem if pageItem else None

    def GeneratePage(self, page):

        self.logger.debug(f"GeneratePage called with {page=}")

        page_content = self.panel_config['cards'][page]

        if page_content['pageType'] == 'cardEntities':
            self.SendToPanel(self.GenerateEntitiesPage(page))
        """
        elif page_content['pageType'] == 'cardThermo':
            SendToPanel(GenerateThermoPage(page))
    
        elif page_content['pageType'] == 'cardGrid':
            SendToPanel(GenerateGridPage(page))
    
        elif page_content['pageType'] == 'cardMedia':
            logic.useMediaEvents = True
            SendToPanel(GenerateMediaPage(page))
    
        elif page_content['pageType'] == 'cardAlarm':
            SendToPanel(GenerateAlarmPage(page))
    
        elif page_content['pageType'] == 'cardQR':
            SendToPanel(GenerateQRPage(page))
    
        elif page_content['pageType'] == 'cardPower':
            SendToPanel(GeneratePowerPage(page))
    
        elif page_content['pageType'] == 'cardChart':
            SendToPanel(GenerateChartPage(page))
        """

    def GenerateDetailPage(self, type: str, pageItem: str):
        self.logger.debug(f"GenerateDetailPage to be implemented")

    def GenerateEntitiesPage(self, page) -> list:
        self.logger.debug(f"GenerateEntitiesPage called with {page=}")
        out_msgs = list()
        out_msgs.append('pageType~cardEntities')
        out_msgs.append({'payload': self.GeneratePageElements(page)})
        return out_msgs

    def GeneratePageElements(self, page) -> str:
        self.logger.debug(f"GeneratePageElements called with {page=}")

        page_content = self.panel_config['cards'][page]

        if page_content['pageType'] in ['cardThermo', 'cardAlarm', 'cardMedia', 'cardQR', 'cardPower', 'cardChart']:
            maxItems = 1
        elif page_content['pageType'] == 'cardEntities':
            maxItems = 4 if self.panel_model == 'eu' else 5
        elif page_content['pageType'] == 'cardGrid':
            maxItems = 6
        else:
            maxItems = 1

        if len(page_content['entities']) > maxItems:
            self.logger.warning(f"Page definition contains too many Entities. Max allowed entities for page={page_content['pageType']} is {maxItems}")

        pageData = f"entityUpd~{page_content['heading']}~{page_content['navigationLeft']}|{page_content['navigationRight']}"

        for idx, entity in enumerate(page_content['entities']):
            self.logger.debug(f"entity={entity}")
            if idx > maxItems:
                break

            item = self._get_item(entity['internalNameEntity'])
            value = item() if item else entity.get('optionalValue', 0)

            pageData = f"{pageData}~{entity['type']}~{entity['internalNameEntity']}~{entity['iconId']}~{entity['iconColor']}~{entity['displayNameEntity']}~{value}"

        return pageData

    def SendToPanel(self, payload):
        self.logger.debug(f"SendToPanel called with {payload=}")

        if isinstance(payload, list):
            for entry in payload:
                self.publish_tasmota_topic(payload=entry)
        else:
            self.publish_tasmota_topic(payload=payload)

    ################################################################
    #  Simulation of mqtt messages of NSPanel
    ################################################################

    def send_lwt_mqtt_msg(self):

        try:
            self.publish_topic(topic=f'tele/{self.tasmota_topic}/LWT', payload='Offline', retain=True)
        except Exception as e:
            return f"Exception during send_lwt_mqtt_msg: {e}"
        else:
            return f"send_lwt_mqtt_msg done"

    def send_discovery_mqtt_msg(self):
        """
        tasmota/discovery/0CDC7E31E4CC/config {"ip":"192.168.178.67","dn":"Tasmota","fn":["Tasmota","",null,null,null,null,null,null],"hn":"NSPanel1-1228","mac":"0CDC7E31E4CC","md":"NSPanel","ty":0,"if":0,"ofln":"Offline","onln":"Online","state":["OFF","ON","TOGGLE","HOLD"],"sw":"12.2.0","t":"NSPanel1","ft":"%prefix%/%topic%/","tp":["cmnd","stat","tele"],"rl":[1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],"swc":[-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1],"swn":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null],"btn":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],"so":{"4":0,"11":0,"13":0,"17":0,"20":0,"30":0,"68":0,"73":0,"82":0,"114":0,"117":0},"lk":0,"lt_st":0,"sho":[0,0,0,0],"sht":[[0,0,0],[0,0,0],[0,0,0],[0,0,0]],"ver":1}
        tasmota/discovery/0CDC7E31E4CC/sensors {"sn":{"Time":"2022-11-28T16:17:32","ANALOG":{"Temperature1":23.2},"ESP32":{"Temperature":28.9},"TempUnit":"C"},"ver":1}
        """

        try:
            self.publish_topic(topic='tasmota/discovery/0CDC7E31E4CC/config', payload='{"ip":"192.168.178.67","dn":"Tasmota","fn":["Tasmota","",null,null,null,null,null,null],"hn":"NSPanel1-1228","mac":"0CDC7E31E4CC","md":"NSPanel","ty":0,"if":0,"ofln":"Offline","onln":"Online","state":["OFF","ON","TOGGLE","HOLD"],"sw":"12.2.0","t":"NSPanel1","ft":"%prefix%/%topic%/","tp":["cmnd","stat","tele"],"rl":[1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],"swc":[-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1],"swn":[null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null,null],"btn":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],"so":{"4":0,"11":0,"13":0,"17":0,"20":0,"30":0,"68":0,"73":0,"82":0,"114":0,"117":0},"lk":0,"lt_st":0,"sho":[0,0,0,0],"sht":[[0,0,0],[0,0,0],[0,0,0],[0,0,0]],"ver":1}', retain=True)
            self.publish_topic(topic='tasmota/discovery/0CDC7E31E4CC/sensors', payload='{"sn":{"Time":"2022-11-28T16:17:32","ANALOG":{"Temperature1":23.2},"ESP32":{"Temperature":28.9},"TempUnit":"C"},"ver":1}', retain=True)
        except Exception as e:
            return f"Exception during send_discovery_mqtt_msg: {e}"
        else:
            return f"send_discovery_mqtt_msg done"

    def send_mqtt_from_nspanel(self, msg_no):

        link = {1: [f'tele/{self.tasmota_topic}/RESULT', '{"CustomRecv": "event,startup,45,eu"}'],
                2: [f'tele/{self.tasmota_topic}/RESULT', '{"CustomRecv": "event,buttonPress2,licht.eg.tv_wand_nische,OnOff,0"}'],
                3: [f'tele/{self.tasmota_topic}/RESULT', '{"CustomRecv": "event,buttonPress2,licht.eg.tv_wand_nische,OnOff,1"}'],
                4: [f'tele/{self.tasmota_topic}/RESULT', '{"CustomRecv": "event,sleepReached,cardEntities"}'],
                5: [f'tele/{self.tasmota_topic}/RESULT', '{"CustomRecv": "event,buttonPress2,screensaver,bExit,1"}'],
                }

        if msg_no not in link:
            return "Message No not defined"

        topic = link.get(msg_no, [f'tele/{self.tasmota_topic}/RESULT', ''])[0]
        payload = link.get(msg_no, ['', ''])[1]

        try:
            self.publish_topic(topic=topic, payload=payload)
        except Exception as e:
            return f"Exception during send_mqtt_from_nspanel: {e}"
        else:
            return f"send_mqtt_from_nspanel with payload={payload} to topic={topic} done"

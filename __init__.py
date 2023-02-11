#!/usr/bin/env python3
# vim: set encoding=utf-8 tabstop=4 softtabstop=4 shiftwidth=4 expandtab
#########################################################################
#  Copyright 2022-      Michael Wenzel            wenzel_michael(a)web.de
#                       Stefan Hauf               stefan.hauf(a)gmail.com
#                       Christian Cordes          info(a)pol3cat.de  
#########################################################################
#  This file is part of SmartHomeNG.
#  https://www.smarthomeNG.de
#  https://knx-user-forum.de/forum/supportforen/smarthome-py
#
#  This plugin connect NSPanel (with tasmota) to SmartHomeNG
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

import colorsys
import math
import os
import queue
import sys
from datetime import datetime, timedelta

import yaml
from lib.item import Items
from lib.model.mqttplugin import MqttPlugin
from lib.shtime import Shtime

from . import nspanel_icons_colors
from .webif import WebInterface

Icons = nspanel_icons_colors.IconsSelector()
Colors = nspanel_icons_colors.ColorThemes()


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

        self.shtime = Shtime.get_instance()
        self.items = Items.get_instance()

        # get the parameters for the plugin (as defined in metadata plugin.yaml):
        try:
            self.webif_pagelength = self.get_parameter_value('webif_pagelength')
            self.tasmota_topic = self.get_parameter_value('topic')
            self.telemetry_period = self.get_parameter_value('telemetry_period')
            self.config_file_location = self.get_parameter_value('config_file_location')
            self.full_topic = self.get_parameter_value('full_topic').lower()
            self.desired_panel_model = self.get_parameter_value('model')
            self.firmware_check = self.get_parameter_value('firmware_check')
            self.brightness = self.get_parameter_value('brightness')
            self.temperatureUnit = self.get_parameter_value('temperatureUnit')
            self.defaultBackgroundColor = self.get_parameter_value('defaultBackgroundColor')
            self.defaultColor = self.get_parameter_value('defaultColor')
            self.defaultOffColor = self.get_parameter_value('defaultOffColor')
            self.defaultOnColor = self.get_parameter_value('defaultOnColor')
            # TODO check if colors are valid otherwise use existing
            pass
        except KeyError as e:
            self.logger.critical(
                "Plugin '{}': Inconsistent plugin (invalid metadata definition: {} not defined)".format(
                    self.get_shortname(), e))
            self._init_complete = False
            return

        # create full_topic
        if self.full_topic.find('%prefix%') == -1 or self.full_topic.find('%topic%') == -1:
            self.full_topic = '%prefix%/%topic%/'
        if self.full_topic[-1] != '/':
            self.full_topic += '/'

        # define properties
        self.current_page = 0
        self.panel_status = {'online': False, 'online_timeout': datetime.now(), 'uptime': '-', 'sensors': {},
                             'relay': {}, 'screensaver_active': False}
        self.custom_msg_queue = queue.Queue(maxsize=50)  # Queue containing last 50 messages containing "CustomRecv"
        self.panel_items = {}
        self.panel_config_items = []
        self.panel_config_items_page = {}
        self.berry_driver_version = 0
        self.display_firmware_version = 0
        self.panel_model = ''
        self.alive = None
        self.lastPayload = []

        # define desired versions
        self.desired_berry_driver_version = 8
        self.display_driver_update = True
        self.desired_display_firmware_version = 48
        self.desired_version = "v3.8.3"
        self.display_display_update = True

        # URLs for display updates
        if self.desired_panel_model == "us-l":
            # us landscape version
            self.desired_display_firmware_url = f"http://nspanel.pky.eu/lovelace-ui/github/nspanel-us-l-{self.desired_version}.tft"
        elif self.desired_panel_model == "us-p":
            # us portrait version
            self.desired_display_firmware_url = f"http://nspanel.pky.eu/lovelace-ui/github/nspanel-us-p-{self.desired_version}.tft"
        else:
            # eu version
            self.desired_display_firmware_url = f"http://nspanel.pky.eu/lovelace-ui/github/nspanel-{self.desired_version}.tft"
        # URL for berry driver update
        self.desired_berry_driver_url = "https://raw.githubusercontent.com/joBr99/nspanel-lovelace-ui/main/tasmota/autoexec.be"

        # read panel config file
        try:
            self.panel_config = self._parse_config_file()
        except Exception as e:
            self.logger.warning(f"Exception during parsing of page config yaml file occurred: {e}")
            self._init_complete = False
            return

        # link items from config to method 'update_item'
        self.get_items_of_panel_config_to_update_item()

        # read locale file
        try:
            self.locale = self._parse_locale_file()
        except Exception as e:
            self.logger.warning(f"Exception during parsing of locals yaml file occurred: {e}")
            self._init_complete = False
            return

        # Add subscription to get device LWT
        self.add_tasmota_subscription('tele', self.tasmota_topic, 'LWT', 'bool', bool_values=['Offline', 'Online'],
                                      callback=self.on_mqtt_lwt_message)
        # Add subscription to get device actions results
        self.add_tasmota_subscription('tele', self.tasmota_topic, 'STATE', 'dict', callback=self.on_mqtt_message)
        self.add_tasmota_subscription('tele', self.tasmota_topic, 'SENSOR', 'dict', callback=self.on_mqtt_message)
        self.add_tasmota_subscription('tele', self.tasmota_topic, 'RESULT', 'dict', callback=self.on_mqtt_message)
        self.add_tasmota_subscription('stat', self.tasmota_topic, 'RESULT', 'dict', callback=self.on_mqtt_message)
        self.add_tasmota_subscription('stat', self.tasmota_topic, 'POWER1', 'num', callback=self.on_mqtt_power_message)
        self.add_tasmota_subscription('stat', self.tasmota_topic, 'POWER2', 'num', callback=self.on_mqtt_power_message)

        # init WebIF
        self.init_webinterface(WebInterface)

        return

    def run(self):
        """
        Run method for the plugin
        """
        self.logger.debug("Run method called")

        self.logger.debug("Check if items from config are available")
        for itemname in self.panel_config_items:
            item = self.items.return_item(itemname)
            if item is None:
                self.logger.error(f"{itemname} is not a valid item. Check configuration")
                # TODO more action necessary?

        # start subscription to all topics
        self.start_subscriptions()

        # set plugin alive
        self.alive = True

    def stop(self):
        """
        Stop method for the plugin
        """
        self.logger.debug("Stop method called")
        self.alive = False

        # stop subscription to all topics
        self.stop_subscriptions()

        # remove scheduler
        self._remove_scheduler()

    def parse_item(self, item):
        """
        Default plugin parse_item method. Is called when the plugin is initialized.
        The plugin can, corresponding to its attribute keywords, decide what to do with
        the item in the future, like adding it to an internal array for future reference
        :param item:    The item to process.
        :return:        If the plugin needs to be informed of an items change you should return a call back function
                        like the function update_item down below. An example when this is needed is the knx plugin
                        where parse_item returns the update_item function when the attribute knx_send is found.
                        This means that when the items value is about to be updated, the call back function is called
                        with the item, caller, source and dest as arguments and in case of the knx plugin the value
                        can be sent to the knx with a knx write function within the knx plugin.
        """
        if self.has_iattr(item.conf, 'nspanel_attr'):
            nspanel_attr = self.get_iattr_value(item.conf, 'nspanel_attr')
            self.logger.info(f"Item={item.id()} identified for NSPanel with nspanel_attr={nspanel_attr}")
            if nspanel_attr:
                nspanel_attr = nspanel_attr.lower()
            else:
                return

            # fill panel_items dict / used for web interface
            self.panel_items[f'item_{nspanel_attr}'] = item

            if nspanel_attr[:5] == 'relay':
                return self.update_item
            if nspanel_attr[:11] == 'screensaver':
                return self.update_item

        # register screensaver update items
        if self.has_iattr(item.conf, 'nspanel_update'):
            return self.update_item

        # search for notify items
        if self.has_iattr(item.conf, 'nspanel_popup'):
            nspanel_popup = self.get_iattr_value(item.conf, 'nspanel_popup')
            self.logger.info(f"parsing item: {item.id()} with nspanel_popup={nspanel_popup}")
            return self.update_item

        if item.property.path in self.panel_config_items:
            return self.update_item

        return None

    def parse_logic(self, logic):
        """
        Default plugin parse_logic method
        """
        if 'xxx' in logic.conf:
            # self.function(logic['name'])
            self.logger.waring('logic not implemented')
            pass

    def update_item(self, item, caller=None, source=None, dest=None):
        """
        Item has been updated
        This method is called, if the value of an item has been updated by SmartHomeNG.
        It should write the changed value out to the device (hardware/interface) that
        is managed by this plugin
        :param item: item to be updated towards the plugin
        :param caller: if given it represents the callers name
        :param source: if given it represents the source
        :param dest: if given it represents the dest
        """
        if self.alive and caller != self.get_shortname():
            # code to execute if the plugin is not stopped
            # and only, if the item has not been changed by this plugin:
            # stop if only update and no change
            if abs(item.property.last_change_age - item.property.last_update_age) > 0.01:
                self.logger.debug(
                    f"update_item was called with item {item.property.path} - no change")
                return
            self.logger.debug(
                f"update_item was called with item {item.property.path} from caller {caller}, source {source} and dest {dest}")

            if self.has_iattr(item.conf, 'nspanel_attr'):
                nspanel_attr = self.get_iattr_value(item.conf, 'nspanel_attr')
                if nspanel_attr[:5] == 'relay':
                    value = item()
                    # check data type
                    if not isinstance(value, bool):
                        return
                    if value is not None:
                        relay = nspanel_attr[5:]
                        self.publish_tasmota_topic('cmnd', self.tasmota_topic, f"POWER{relay}", value, item,
                                                   bool_values=['OFF', 'ON'])

            # Update screensaver, if active
            if self.has_iattr(item.conf, 'nspanel_update') and self.panel_status['screensaver_active']:
                nspanel_update = self.get_iattr_value(item.conf, 'nspanel_update')
                if nspanel_update == 'weather':
                    self.HandleScreensaverWeatherUpdate()
                if nspanel_update == 'status':
                    self.HandleScreensaverIconUpdate()
                if nspanel_update == 'time':
                    self.send_current_time()

            elif self.has_iattr(item.conf, 'nspanel_popup'):
                nspanel_popup = self.get_iattr_value(item.conf, 'nspanel_popup')
                if nspanel_popup[:6] == 'notify':
                    item_value = item()
                    if isinstance(item_value, dict):
                        if nspanel_popup[6:] == '_screensaver':
                            self.SendToPanel(self.GenerateScreensaverNotify(item_value))
                        else:
                            self.SendToPanel(self.GeneratePopupNotify(item_value))
                    else:
                        self.logger.warning(f"{item.id} must be a dict")
                elif self.get_iattr_value(item.conf, 'nspanel_popup') == 'timer':
                    entities = self.panel_config['cards'][self.current_page]['entities']
                    entity_name = next(
                        (entity['entity'] for entity in entities if entity.get('item', '') == item.property.path), None)
                    if entity_name is not None:
                        self.SendToPanel(self.GenerateDetailTimer(entity_name))
            elif not self.panel_status['screensaver_active']:
                if item.property.path in self.panel_config_items_page[self.current_page]:
                    self.GeneratePage(self.current_page)
                else:
                    self.logger.debug(f"item not on current_page = {self.current_page}")
            else:
                self.logger.debug(f"screensaver active")
            pass

    ################################
    # CallBacks
    ################################

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
            self.logger.info(
                f"Received LWT Message for {tasmota_topic} with payload={payload}, qos={qos} and retain={retain}")
        except Exception as e:
            self.logger.error(f"received topic {topic} is not in correct format. Error was: {e}")
        else:

            if payload:
                self.panel_status['online_timeout'] = datetime.now() + timedelta(seconds=self.telemetry_period + 5)
                self.panel_status['online'] = payload
                self._set_item_value('item_online', payload)
                self._add_scheduler()
                self.publish_tasmota_topic('cmnd', self.tasmota_topic, 'GetDriverVersion', 'x')
                self.SendToPanel('pageType~pageStartup')
                # set telemetry to get the latest STATE and SENSOR information
                self._set_telemetry_period(self.telemetry_period)
            else:
                self._set_device_offline()

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
            self.logger.debug(
                f"on_mqtt_message: topic_type={topic_type}, tasmota_topic={tasmota_topic}, info_topic={info_topic}, payload={payload}, qos={qos} and retain={retain}")
        except Exception as e:
            self.logger.error(f"received topic {topic} is not in correct format. Error was: {e}")
        else:

            # handle message
            if isinstance(payload, dict) and info_topic in ['STATE', 'RESULT']:

                # Handling of Driver Version
                if 'nlui_driver_version' in payload:
                    self.logger.info(f"Received Message decoded as driver version message.")
                    self.berry_driver_version = payload['nlui_driver_version']

                # Handling of TelePeriod
                if 'TelePeriod' in payload:
                    self.logger.info(f"Received Message decoded as teleperiod message.")
                    self._handle_teleperiod(payload['TelePeriod'])

                # Handling of CustomRecv messages
                elif 'CustomRecv' in payload:
                    self.logger.info(
                        f"Received Message decoded as NSPanel Message, will be put to queue for logging reasons. {self.custom_msg_queue.qsize() + 1} messages logged.")
                    self.custom_msg_queue.put(payload['CustomRecv'])
                    self.HandlePanelMessage(payload['CustomRecv'])

                # Handling of Power messages
                elif any(item.startswith("POWER") for item in payload.keys()):
                    self.logger.info(f"Received Message decoded as power message.")
                    self._handle_power(payload)

                # Handling of Wi-Fi
                if 'Wifi' in payload:
                    self.logger.info(f"Received Message contains Wifi information.")
                    self._handle_wifi(payload['Wifi'])

                # Handling of Uptime
                if 'Uptime' in payload:
                    self.logger.info(f"Received Message contains Uptime information.")
                    self._handle_uptime(payload['Uptime'])

            elif isinstance(payload, dict) and info_topic == 'SENSOR':
                self.logger.info(f"Received Message contains sensor information.")
                self._handle_sensor(payload)

            else:
                self.logger.warning(f"Received Message '{payload}' not handled within plugin.")

            # setting new online-timeout
            self.panel_status['online_timeout'] = datetime.now() + timedelta(seconds=self.telemetry_period + 5)

    def on_mqtt_power_message(self, topic: str, payload: dict, qos: int = None, retain: bool = None) -> None:
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
                f"on_mqtt_power_message: topic_type={topic_type}, tasmota_topic={tasmota_topic}, info_topic={info_topic}, payload={payload}, qos={qos} and retain={retain}")
        except Exception as e:
            self.logger.error(f"received topic {topic} is not in correct format. Error was: {e}")
        else:
            if info_topic.startswith('POWER'):
                tasmota_relay = str(info_topic[5:])
                tasmota_relay = '1' if not tasmota_relay else None
                item_relay = f'item_relay{tasmota_relay}'
                self._set_item_value(item_relay, payload == 'ON')
                self.panel_status['relay'][info_topic] = payload

    ################################
    # MQTT Stuff
    ################################

    def publish_tasmota_topic(self, prefix: str = 'cmnd', topic: str = None, detail: str = None, payload: any = None,
                              item=None, qos: int = None, retain: bool = False, bool_values: list = None) -> None:
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

        # self.logger.debug(f"publish_topic with tpc={tpc}, payload={payload}")
        self.publish_topic(tpc, payload, item, qos, retain, bool_values)

    def add_tasmota_subscription(self, prefix: str, topic: str, detail: str, payload_type: str,
                                 bool_values: list = None, callback=None) -> None:
        """
        build the topic in Tasmota style and add the subscription to mqtt
        :param prefix:          prefix of topic to subscribe to
        :param topic:           unique part of topic to subscribe to
        :param detail:          detail of topic to subscribe to
        :param payload_type:    payload type of the topic (for this subscription to the topic)
        :param bool_values:     bool values (for this subscription to the topic)
        :param callback:        a plugin can provide an own callback function, if special handling of the payload is needed
        """

        tpc = self.full_topic.replace("%prefix%", prefix)
        tpc = tpc.replace("%topic%", topic)
        tpc += detail
        self.add_subscription(tpc, payload_type, bool_values=bool_values, callback=callback)

    ################################
    # Tasmota Stuff
    ################################

    def _set_item_value(self, itemtype: str, value) -> None:
        """
        Sets item value
        :param itemtype:        itemtype to be set
        :param value:           value to be set
        """

        if itemtype in self.panel_items:
            # get item to be set
            item = self.panel_items[itemtype]

            # set item value
            self.logger.info(
                f"{self.tasmota_topic}: Item '{item.id()}' via itemtype '{itemtype}' set to value '{value}'.")
            item(value, self.get_shortname())

        else:
            self.logger.debug(
                f"{self.tasmota_topic}: No item for itemtype '{itemtype}' defined to set to '{value}'.")

    def _set_device_offline(self):
        self._set_item_value('item_online', False)
        self.logger.info(
            f"{self.tasmota_topic} is not online any more - online_timeout={self.panel_status['online_timeout']}, now={datetime.now()}")

        # clean data to show correct status
        self.panel_status['online_timeout'] = datetime.now()
        self.panel_status['online'] = False
        self.panel_status['screensaver_active'] = False
        self.panel_status['uptime'] = '-'
        self.panel_status['wifi_signal'] = 0
        self.panel_status['sensors'].clear()
        self.panel_status['relay'].clear()

        self._remove_scheduler()

    def _check_online_status(self):
        """
        checks all tasmota topics, if last message is with telemetry period. If not set tasmota_topic offline
        """

        self.logger.info(f"_check_online_status: Checking online status of {self.tasmota_topic}")
        if self.panel_status.get('online') is True and self.panel_status.get('online_timeout'):
            if self.panel_status['online_timeout'] < datetime.now():
                self._set_device_offline()
            else:
                self.logger.debug(f'_check_online_status: Checking online status of {self.tasmota_topic} successful')

    def _set_telemetry_period(self, telemetry_period: int) -> None:
        """
        sets telemetry period for given topic
        """

        self.logger.info(f"run: Setting telemetry period to {telemetry_period} seconds")
        self.publish_tasmota_topic('cmnd', self.tasmota_topic, 'teleperiod', telemetry_period)

    def _handle_wifi(self, payload: dict) -> None:
        """
        Extracts Wi-Fi information out of payload and updates plugin dict
        :param payload:         MQTT message payload
        """
        self.logger.debug(f"_handle_wifi: received payload={payload}")
        wifi_signal = payload.get('Signal')
        if wifi_signal:
            if isinstance(wifi_signal, str) and wifi_signal.isdigit():
                wifi_signal = int(wifi_signal)
            self.panel_status['wifi_signal'] = wifi_signal
            self._set_item_value('item_wifi_signal', wifi_signal)

    def _handle_teleperiod(self, teleperiod: dict) -> None:

        self.panel_status['teleperiod'] = teleperiod
        if teleperiod != self.telemetry_period:
            self._set_telemetry_period(self.telemetry_period)

    def _handle_uptime(self, uptime: str) -> None:
        self.logger.debug(f"Received Message contains Uptime information. uptime={uptime}")
        self.panel_status['uptime'] = uptime
        self._set_item_value('item_uptime', uptime)

    def _handle_power(self, payload: dict) -> None:
        """
        Extracts Power information out of payload and updates plugin dict
        :param payload:         MQTT message payload
        """
        # payload = {"Time": "2022-11-21T12:56:34", "Uptime": "0T00:00:11", "UptimeSec": 11, "Heap": 27, "SleepMode": "Dynamic", "Sleep": 50, "LoadAvg": 19, "MqttCount": 0, "POWER1": "OFF", "POWER2": "OFF", "POWER3": "OFF", "POWER4": "OFF", "Wifi": {"AP": 1, "SSId": "WLAN-Access", "BSSId": "38:10:D5:15:87:69", "Channel": 1, "Mode": "11n", "RSSI": 82, "Signal": -59, "LinkCount": 1, "Downtime": "0T00:00:03"}}

        power_dict = {key: val for key, val in payload.items() if key.startswith('POWER')}
        self.panel_status['relay'].update(power_dict)
        for power in power_dict:
            relay_index = 1 if len(power) == 5 else str(power[5:])
            item_relay = f'item_relay{relay_index}'
            self._set_item_value(item_relay, power_dict[power])

    def _handle_sensor(self, payload: dict) -> None:
        """
        :param payload:
        :return:
        """

        # tele / NSPanel1 / SENSOR = {"Time": "2022-12-03T13:21:26", "ANALOG": {"Temperature1": 28.0}, "ESP32": {"Temperature": 38.9}, "TempUnit": "C"}

        for sensor in self.TEMP_SENSOR:
            data = payload.get(sensor)

            if data and isinstance(data, dict):
                self.logger.info(f"Received Message decoded as {sensor} Sensor message.")
                if sensor not in self.panel_status['sensors']:
                    self.panel_status['sensors'][sensor] = {}

                for key in self.TEMP_SENSOR_KEYS:
                    if key in data:
                        self.panel_status['sensors'][sensor][key.lower()] = data[key]
                        self._set_item_value(self.TEMP_SENSOR_KEYS[key], data[key])

    ################################
    #  NSPage Stuff
    ################################

    def _add_scheduler(self):
        """
        add scheduler for cyclic time and date update
        """

        self.logger.debug('Add scheduler for cyclic updates of time and date')

        dt = self.shtime.now() + timedelta(seconds=20)
        self.scheduler_add('update_time', self.send_current_time, next=dt, cycle=60)
        self.scheduler_add('update_date', self.send_current_date, cron='1 0 0 * * *', next=dt)

        self.logger.debug(f"Add scheduler for online_status")
        dt = self.shtime.now() + timedelta(seconds=(self.telemetry_period - 3))
        self.scheduler_add('check_online_status', self._check_online_status, cycle=self.telemetry_period, next=dt)

    def _remove_scheduler(self):
        """
        remove scheduler for cyclic time and date update
        """

        self.logger.debug('Remove scheduler for cyclic updates of time and date')

        self.scheduler_remove('update_time')
        self.scheduler_remove('update_date')

        self.logger.debug('Remove scheduler for online status')
        self.scheduler_remove('check_online_status')

    def _parse_config_file(self):
        """
        Parse the page config file and check for completeness
        """

        with open(self.config_file_location, 'r') as stream:
            try:
                config = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                self.logger.warning(f"Exception during parsing of page config yaml file occurred: {exc}")
                return None

        # ToDo: Für alle Pages die Anzahl der Entities prüfen
        # ToDo: Für alle Pages die Konfig auf Vollständigkeit prüfen

        self.logger.debug(f"_parse_config_file: page-config={config} available!")
        return config

    def _parse_locale_file(self):
        """
        Parse the locals file and check for completeness
        """
        with open(os.path.join(sys.path[0], "plugins", self.get_shortname(), "locale.yaml"), "r") as stream:
            try:
                locale_dict = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                self.logger.warning(f"Exception during parsing of locale yaml file occurred: {exc}")
                return None

        self.logger.debug(f"_parse_locale_file: locale={locale_dict} available!")
        return locale_dict

    def get_items_of_panel_config_to_update_item(self):
        """
        Put all item out of config file to update_item
        """

        for idx, card in enumerate(self.panel_config['cards']):
            self.panel_config_items_page[idx] = []
            temp = []
            entities = card.get('entities')
            if entities is not None:
                for entity in entities:
                    item = entity.get('item', None)
                    # Add all possible items without check, parse_item is only called for valid items
                    if item is not None and item != '' and item not in temp:
                        temp.append(item)
                        if item not in self.panel_config_items:
                            self.panel_config_items.append(item)

            for element in card:
                if element[:4] == 'item':
                    item = card.get(element, None)
                    if item is not None and item != '' and item not in temp:
                        temp.append(item)
                        if item not in self.panel_config_items:
                            self.panel_config_items.append(item)

            self.panel_config_items_page[idx] = temp

    def _next_page(self):
        """
        return next_page number
        """

        self.current_page += 1
        if self.current_page >= len(self.panel_config['cards']):
            self.current_page -= len(self.panel_config['cards'])
        self.logger.debug(f"next_page={self.current_page}")

    def _previous_page(self):
        """
        return previous_page number
        """

        self.current_page -= 1
        if self.current_page < 0:
            self.current_page += len(self.panel_config['cards'])
        self.logger.debug(f"previous_page={self.current_page}")

    def _get_locale(self, group, entry):
        return self.locale.get(group, {}).get(entry, {}).get('de-DE')  # TODO configure in plugin.yaml

    def send_current_time(self):
        secondLineItem = self.panel_config.get('screensaver', {}).get('itemSecondLine', '')
        secondLine = self.items.return_item(secondLineItem)()
        if secondLine is None:
            secondLine = ''
        timeFormat = self.panel_config.get('screensaver', {}).get('timeFormat', "%H:%M")
        self.publish_tasmota_topic(payload=f"time~{self.shtime.now().strftime(timeFormat)}~{secondLine}")

    def send_current_date(self):
        dateFormat = self.panel_config.get('screensaver', {}).get('dateFormat', "%A, %-d. %B %Y")
        # replace some variables to get localized strings
        dateFormat = dateFormat.replace('%A',
                                        self.shtime.weekday_name())  # TODO add code after merge in main repository .replace('%B', self.shtime.current_monthname())
        self.publish_tasmota_topic(payload=f"date~{self.shtime.now().strftime(dateFormat)}")

    def send_screensavertimeout(self):
        screensavertimeout = self.panel_config.get('screensaver', {}).get('timeout', 10)
        self.publish_tasmota_topic(payload=f"timeout~{screensavertimeout}")

    def send_panel_brightness(self):
        brightness_screensaver = self.panel_config.get('screensaver', {}).get('brightness', 10)
        brightness_active = self.brightness
        dbc = rgb_dec565(getattr(Colors, self.defaultBackgroundColor))
        # same value for both values will break sleep timer of the firmware # comment from HA code
        if brightness_screensaver == brightness_active:
            brightness_screensaver = brightness_screensaver - 1
        self.publish_tasmota_topic(payload=f"dimmode~{brightness_screensaver}~{brightness_active}~{dbc}")

    def HandlePanelMessage(self, payload: str) -> None:
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
        event,buttonPress2,item,up
        event,buttonPress2,item,down
        event,buttonPress2,item,stop
        event,buttonPress2,item,OnOff,1
        event,buttonPress2,item,button
        # popupLight Page
        event,pageOpenDetail,popupLight,item
        event,buttonPress2,item,OnOff,1
        event,buttonPress2,item,brightnessSlider,50
        event,buttonPress2,item,colorTempSlider,50
        event,buttonPress2,item,colorWheel,x|y|wh
        # popupShutter Page
        event,pageOpenDetail,popupShutter,item
        event,buttonPress2,item,positionSlider,50
        # popupNotify Page
        event,buttonPress2,*internalName*,notifyAction,yes
        event,buttonPress2,*internalName*,notifyAction,no
        # cardThermo Page
        event,buttonPress2,*entityName*,tempUpd,*temperature*
        event,buttonPress2,*entityName*,hvac_action,*hvac_action*
        # cardMedia Page
        event,buttonPress2,item,media-back
        event,buttonPress2,item,media-pause
        event,buttonPress2,item,media-next
        event,buttonPress2,item,volumeSlider,75
        # cardAlarm Page
        event,buttonPress2,item,actionName,code
        """

        try:
            content_list = payload.split(',')
        except Exception as e:
            self.logger.warning(f"During handling of payload exception occurred: {e}")
            return None

        self.logger.debug(
            f"HandlePanelMessage: content_list={content_list}, length of content_list={len(content_list)}")

        typ = content_list[0]
        method = content_list[1]
        words = content_list

        if typ == 'event':
            if method == 'startup':
                self.display_firmware_version = words[2]
                self.panel_model = words[3]
                self.send_screensavertimeout()
                self.send_panel_brightness()

                if self.firmware_check == 'notify':
                    # Check driver version
                    if int(self.berry_driver_version) < self.desired_berry_driver_version:
                        self.logger.warning(
                            f"Update of Tasmota Driver needed! installed: {self.berry_driver_version} required: {self.desired_berry_driver_version}")
                        if self.display_driver_update:
                            update_message = {
                                "entity": "driverUpdate",
                                "heading": "Driver Update available!",
                                "text": ("There's an update available for the Tasmota\r\n"
                                         "Berry driver, do you want to start the update\r\n"
                                         "now?\r\n"
                                         "If you encounter issues after the update or\r\n"
                                         "this message appears frequently, please check\r\n"
                                         "the manual and repeat the installation steps\r\n"
                                         "for the Tasmota Berry driver."
                                         ),
                                "buttonLeft": "Dismiss",
                                "buttonRight": "Update",
                                "timeout": 0,
                            }
                            self.SendToPanel(self.GeneratePopupNotify(update_message))
                        self.display_driver_update = False

                    # Check panel model
                    elif self.panel_model != self.desired_panel_model:
                        self.logger.warning(
                            f"Update of Display Firmware needed! installed: {self.panel_model} configured: {self.desired_panel_model}")
                        if self.display_display_update:
                            update_message = {
                                "entity": "displayUpdate",
                                "heading": "Display Update available!",
                                "text": ("The configured model does not match to the\r\n"
                                         "installed firmware. Possible solutions:\r\n"
                                         f"- Set model to '{self.panel_model}' in configuration\r\n"
                                         "- Update the correct display firmware\r\n"
                                         "If the update fails check the installation manu-\r\n"
                                         "al and flash again over the Tasmota console\r\n"
                                         "Be patient, the update will take a while.\r\n"
                                         ),
                                "buttonLeft": "Dismiss",
                                "buttonRight": "Update",
                                "timeout": 0,
                            }
                            self.SendToPanel(self.GeneratePopupNotify(update_message))

                    # Check display firmware version
                    elif int(self.display_firmware_version) < self.desired_display_firmware_version:
                        self.logger.warning(
                            f"Update of Display Firmware needed! installed: {self.display_firmware_version} required: {self.desired_display_firmware_version}")
                        if self.display_display_update:
                            update_message = {
                                "entity": "displayUpdate",
                                "heading": "Display Update available!",
                                "text": ("There's a firmware update available for the\r\n"
                                         "Nextion screen of the NSPanel. Do you want to\r\n"
                                         "start the update now?\r\n"
                                         "If the update fails check the installation manu-\r\n"
                                         "al and flash again over the Tasmota console\r\n"
                                         "Be patient, the update will take a while."
                                         ),
                                "buttonLeft": "Dismiss",
                                "buttonRight": "Update",
                                "timeout": 0,
                            }
                            self.SendToPanel(self.GeneratePopupNotify(update_message))
                        self.display_display_update = False
                    else:
                        # Normal startup
                        self.HandleScreensaver()
                else:
                    # startup without check
                    self.HandleScreensaver()

            elif method == 'sleepReached':
                # event,sleepReached,cardEntities
                self.HandleScreensaver()

            elif method == 'pageOpenDetail':
                # event,pageOpenDetail,popupLight,entity
                self.GenerateDetailPage(words[2], words[3])

            elif method == 'buttonPress2':
                self.HandleButtonEvent(words)

            elif method == 'button1':
                self.HandleHardwareButton(method)

            elif method == 'button2':
                self.HandleHardwareButton(method)

    def HandleScreensaver(self):
        self.panel_status['screensaver_active'] = True
        self._set_item_value('item_screensaver_active', self.panel_status['screensaver_active'])
        self.current_page = 0
        self.lastPayload = [""]
        self.publish_tasmota_topic(payload="pageType~screensaver")
        self.send_current_time()
        self.send_current_date()
        self.HandleScreensaverWeatherUpdate()
        self.HandleScreensaverIconUpdate()

    def get_status_icons(self) -> str:
        self.logger.debug("get_status_icons called")
        screensaver = self.panel_config.get('screensaver', {})
        iconLeft = self.items.return_item(screensaver.get('statusIconLeft', None))()
        iconRight = self.items.return_item(screensaver.get('statusIconRight', None))()
        iconSize = screensaver.get('statusIconBig', True)
        if iconSize:
            iconSize = 1
        else:
            iconSize = ''

        # Left Icon
        icon1 = ''
        icon1Color = 'White'
        if isinstance(iconLeft, dict):
            icon1 = Icons.GetIcon(iconLeft.get('icon', ''))
            icon1Color = rgb_dec565(getattr(Colors, iconLeft.get('color', 'White')))
        # Right Icon
        icon2 = ''
        icon2Color = 'White'
        if isinstance(iconRight, dict):
            icon2 = Icons.GetIcon(iconRight.get('icon', ''))
            icon2Color = rgb_dec565(getattr(Colors, iconRight.get('color', 'White')))
        icon1Font = iconSize
        icon2Font = iconSize
        return f"{icon1}~{icon1Color}~{icon2}~{icon2Color}~{icon1Font}~{icon2Font}"

    def HandleScreensaverIconUpdate(self):
        self.logger.info('Function HandleScreensaverIconUpdate')
        status_icons = self.get_status_icons()
        self.publish_tasmota_topic(payload=f"statusUpdate~{status_icons}")

    def getWeatherIcon(self, icon, day: bool = True):
        """Get weather icon from weather data."""
        weatherMapping = {
            'clear_night': 'weather-night',
            'cloudy': 'weather-cloudy',
            'exceptional': 'alert-circle-outline',
            'fog': 'weather-fog',
            'hail': 'weather-hail',
            'lightning': 'weather-lightning',
            'lightning_rainy': 'weather-lightning-rainy',
            'partlycloudy': 'weather-partly-cloudy',
            'pouring': 'weather-pouring',
            'rainy': 'weather-rainy',
            'snowy': 'weather-snowy',
            'snowy_rainy': 'weather-snowy-rainy',
            'sunny': 'weather-sunny',
            'windy': 'weather-windy',
            'windy_variant': 'weather-windy-variant'
        }
        self.logger.debug(f"getWeatherIcon called with icon={icon}")
        # iconname
        if isinstance(icon, str):
            return Icons.GetIcon(icon)
        # numeric value
        elif (isinstance(icon, int) or isinstance(icon, float)) and icon < 100:
            return round(icon, 1)
        # Handle OWM weather code
        elif isinstance(icon, int):
            weatherCondition = getWeatherCondition(icon, day)[0]
            if weatherCondition:
                return Icons.GetIcon(weatherMapping[weatherCondition])
            else:
                self.logger.warning('unknown openweathermap weather code')
                return icon

    def getWeatherAutoColor(self, icon, day: bool = True):
        default_weather_icon_color_mapping = {
            "clear_night": "35957",  # 50% grey
            "cloudy": "31728",  # grey-blue
            "exceptional": "63488",  # red
            "fog": "21130",  # 75% grey
            "hail": "65535",  # white
            "lightning": "65120",  # golden-yellow
            "lightning_rainy": "50400",  # dark-golden-yellow
            "partlycloudy": "35957",  # 50% grey
            "pouring": "249",  # blue
            "rainy": "33759",  # light-blue
            "snowy": "65535",  # white
            "snowy_rainy": "44479",  # light-blue-grey
            "sunny": "63469",  # bright-yellow
            "windy": "35957",  # 50% grey
            "windy_variant": "35957"  # 50% grey
        }
        self.logger.debug(f"getWeatherAutoColor called with icon={icon}")
        if isinstance(icon, int):
            weatherCondition = getWeatherCondition(icon, day)[0]
            if weatherCondition:
                return default_weather_icon_color_mapping[weatherCondition]

        return rgb_dec565(getattr(Colors, self.defaultColor))

    def HandleScreensaverWeatherUpdate(self):
        self.logger.info('Function HandleScreensaverWeatherUpdate')
        weather = self.panel_config.get('screensaver', {}).get('weather', {})

        if weather:
            # actual weather
            tMainIcon = self.items.return_item(weather[0].get('icon'))()
            tMainText = self.items.return_item(weather[0].get('text'))()
            cMainIcon = self.getWeatherAutoColor(tMainIcon, self.items.return_item('env.location.day')())
            optionalLayoutIcon = ""
            optionalLayoutText = ""
            optionalLayoutIconColor = ""
            if weather[0].get('alternativeLayout', False):
                optionalLayoutIconItem = self.items.return_item(weather[0].get('second_icon'))
                if optionalLayoutIconItem is not None:
                    optionalLayoutIcon = optionalLayoutIconItem()
                optionalLayoutIconColor = rgb_dec565(getattr(Colors, self.defaultColor))
                optionalLayoutText = self.items.return_item(weather[0].get('second_text'))()

            # forecast day 1
            tForecast1 = self.items.return_item(weather[1].get('day'))()
            tF1Icon = self.items.return_item(weather[1].get('icon'))()
            tForecast1Val = self.items.return_item(weather[1].get('text'))()
            cF1Icon = self.getWeatherAutoColor(tF1Icon)

            # forecast day 2
            tForecast2 = self.items.return_item(weather[2].get('day'))()
            tF2Icon = self.items.return_item(weather[2].get('icon'))()
            tForecast2Val = self.items.return_item(weather[2].get('text'))()
            cF2Icon = self.getWeatherAutoColor(tF2Icon)

            # forecast day 3
            tForecast3 = self.items.return_item(weather[3].get('day'))()
            tF3Icon = self.items.return_item(weather[3].get('icon'))()
            tForecast3Val = self.items.return_item(weather[3].get('text'))()
            cF3Icon = self.getWeatherAutoColor(tF3Icon)

            # forecast day 4
            tForecast4 = self.items.return_item(weather[4].get('day'))()
            tF4Icon = self.items.return_item(weather[4].get('icon'))()
            tForecast4Val = self.items.return_item(weather[4].get('text'))()
            cF4Icon = self.getWeatherAutoColor(tF4Icon)

            out_msgs = list()
            out_msgs.append(f"weatherUpdate~"
                            f"ignore~"
                            f"ignore~"
                            f"{self.getWeatherIcon(tMainIcon, self.items.return_item('env.location.day')())}~"
                            f"{cMainIcon}~"
                            f"ignore~"
                            f"{tMainText}~"
                            f"ignore~"
                            f"ignore~"
                            f"{self.getWeatherIcon(tF1Icon)}~"
                            f"{cF1Icon}~"
                            f"{tForecast1}~"
                            f"{tForecast1Val}~"
                            f"ignore~"
                            f"ignore~"
                            f"{self.getWeatherIcon(tF2Icon)}~"
                            f"{cF2Icon}~"
                            f"{tForecast2}~"
                            f"{tForecast2Val}~"
                            f"ignore~"
                            f"ignore~"
                            f"{self.getWeatherIcon(tF3Icon)}~"
                            f"{cF3Icon}~"
                            f"{tForecast3}~"
                            f"{tForecast3Val}~"
                            f"ignore~"
                            f"ignore~"
                            f"{self.getWeatherIcon(tF4Icon)}~"
                            f"{cF4Icon}~"
                            f"{tForecast4}~"
                            f"{tForecast4Val}~"
                            f"ignore~"
                            f"ignore~"
                            f"{Icons.GetIcon(optionalLayoutIcon)}~"
                            f"{optionalLayoutIconColor}~"
                            f"ignore~"
                            f"{optionalLayoutText}"
                            )

            # set colors
            background = rgb_dec565(getattr(Colors, self.defaultBackgroundColor))
            timestr = rgb_dec565(getattr(Colors, self.defaultColor))
            timeAPPM = rgb_dec565(getattr(Colors, self.defaultColor))
            date = rgb_dec565(getattr(Colors, self.defaultColor))
            cMainText = rgb_dec565(getattr(Colors, self.defaultColor))
            cForecast1 = rgb_dec565(getattr(Colors, self.defaultColor))
            cForecast2 = rgb_dec565(getattr(Colors, self.defaultColor))
            cForecast3 = rgb_dec565(getattr(Colors, self.defaultColor))
            cForecast4 = rgb_dec565(getattr(Colors, self.defaultColor))
            cForecast1Val = rgb_dec565(getattr(Colors, self.defaultColor))
            cForecast2Val = rgb_dec565(getattr(Colors, self.defaultColor))
            cForecast3Val = rgb_dec565(getattr(Colors, self.defaultColor))
            cForecast4Val = rgb_dec565(getattr(Colors, self.defaultColor))
            bar = rgb_dec565(getattr(Colors, self.defaultColor))
            tMR = rgb_dec565(getattr(Colors, self.defaultColor))
            tTimeAdd = rgb_dec565(getattr(Colors, self.defaultColor))

            out_msgs.append(f'color~{background}~'
                            f'{timestr}~'
                            f'{timeAPPM}~'
                            f'{date}~'
                            f'{cMainText}~'
                            f'{cForecast1}~'
                            f'{cForecast2}~'
                            f'{cForecast3}~'
                            f'{cForecast4}~'
                            f'{cForecast1Val}~'
                            f'{cForecast2Val}~'
                            f'{cForecast3Val}~'
                            f'{cForecast4Val}~'
                            f'{bar}~'
                            f'{tMR}~'
                            f'{tMR}~'
                            f'{tTimeAdd}'
                            )
            self.SendToPanel(out_msgs)

    def GenerateScreensaverNotify(self, value) -> list:
        self.logger.debug(f"GenerateScreensaverNotify called with item={value}")

        if not self.panel_status['screensaver_active']:
            self.HandleScreensaver()

        heading = value.get('heading', '')
        text = value.get('text', '')
        out_msgs = list()
        out_msgs.append(f"notify~{heading}~{text}")
        return out_msgs

    def HandleHardwareButton(self, method):
        self.logger.info(f"hw {method} pressed")
        # TODO switch to hidden page
        # TODO direct toggle item
        self.GeneratePage(self.current_page)

    def getEntityByName(self, name: str = ""):
        entities = self.panel_config['cards'][self.current_page]['entities']
        entity = next((entity for entity in entities if entity["entity"] == name), None)
        return entity

    def getPageByName(self, name: str = ""):
        cards = self.panel_config['cards']
        for idx, card in enumerate(cards):
            if card.get("entity", None) == name:
                return idx

    def HandleButtonEvent(self, words):

        # words=['event', 'buttonPress2', 'licht.eg.tv_wand_nische', 'OnOff', '1']

        pageName = words[2]
        buttonAction = words[3]

        self.logger.debug(
            f"HandleButtonEvent: {words[0]} - {words[1]} - {words[2]} - {words[3]} - current_page={self.current_page}")

        if 'navigate' in pageName:
            self.GeneratePage(pageName[8:len(pageName)])

        if buttonAction == 'bExit':
            if pageName == 'popupNotify' and self.panel_status['screensaver_active']:
                self.HandleScreensaver()
            else:
                self.lastPayload = [""]
                self.GeneratePage(self.current_page)

        elif buttonAction == 'OnOff':
            value = int(words[4])
            entity = self.getEntityByName(pageName)
            item_name = entity.get('item', '')
            item = self.items.return_item(item_name)
            if item is not None:
                value = entity.get('onValue', 1) if value else entity.get('offValue', 0)
                self.logger.debug(f"item={item.id()} will be set to new value={value}")
                item(value, self.get_shortname())

        elif buttonAction == 'number-set' or buttonAction == 'positionSlider' or buttonAction == 'tiltSlider':
            self.logger.debug(f"{buttonAction} called with with pageName={pageName}")
            value = int(words[4])
            entity = self.getEntityByName(pageName)
            itemconfigname = 'item'
            scaled_value = value  # no scaling for number-set
            if buttonAction == 'positionSlider':
                itemconfigname = 'item_pos'
                min_value = entity.get('min_pos', 0)
                max_value = entity.get('max_pos', 100)
                scaled_value = scale(value, (0, 100), (min_value, max_value))
            elif buttonAction == 'tiltSlider':
                itemconfigname = 'item_tilt'
                min_value = entity.get('min_tilt', 0)
                max_value = entity.get('max_tilt', 100)
                scaled_value = scale(value, (0, 100), (min_value, max_value))
            elif entity.get('type') == 'fan' and buttonAction == 'number-set':
                itemconfigname = 'item_speed'
                scaled_value = value * entity.get("percentage_step", 25)

            item = self.items.return_item(entity.get(itemconfigname, None))
            if item is not None:
                self.logger.debug(f"item={item.id()} will be set to new scaled_value={scaled_value}")
                item(scaled_value, self.get_shortname())

        elif buttonAction == 'brightnessSlider':
            value = int(words[4])
            self.logger.debug(f"brightnessSlider called with pageName={pageName}")
            entity = self.getEntityByName(pageName)
            item = self.items.return_item(entity.get('item_brightness', None))
            scaled_value = scale(value, (0, 100),
                                 (entity.get('min_brightness', "0"), entity.get('max_brightness', "100")))
            if item is not None:
                self.logger.debug(f"item={item.id()} will be set to new scaled_value={scaled_value}")
                item(scaled_value, self.get_shortname())

        elif buttonAction == 'colorTempSlider':
            value = int(words[4])
            self.logger.debug(f"colorTempSlider called with pageName={pageName}")
            entity = self.getEntityByName(pageName)
            item = self.items.return_item(entity.get('item_temperature', None))
            scaled_value = scale(value, (100, 0),
                                 (entity.get('min_temperature', "0"), entity.get('max_temperature', "100")))
            if item is not None:
                self.logger.debug(f"item={item.id()} will be set to new scaled_value={scaled_value}")
                item(scaled_value, self.get_shortname())

        elif buttonAction == 'colorWheel':
            value = words[4]
            self.logger.debug(f"colorWheel called with pageName={pageName}")
            entity = self.getEntityByName(pageName)
            item = self.items.return_item(entity.get('item_color', None))
            value = value.split('|')
            rgb = pos_to_color(int(value[0]), int(value[1]), int(value[2]))
            red = rgb[0]
            blue = rgb[1]
            green = rgb[2]
            if item is not None:
                item(f"[{red}, {blue}, {green}]", self.get_shortname())
                self.logger.debug(f"item={item.id()} will be set to red={red} blue={blue} green={green}")

        elif buttonAction == 'bNext':
            self._next_page()
            self.GeneratePage(self.current_page)

        elif buttonAction == 'bPrev':
            self._previous_page()
            self.GeneratePage(self.current_page)

        elif buttonAction == 'button':
            self.logger.debug(f"button called with pageName={pageName}")
            if pageName == '':
                self.logger.warning('no pageName given')

            elif pageName == 'bHome':
                self.current_page = 0
                self.GeneratePage(self.current_page)

            elif pageName == 'bNext':
                self._next_page()
                self.GeneratePage(self.current_page)

            elif pageName == 'bPrev':
                self._previous_page()
                self.GeneratePage(self.current_page)

            elif pageName == 'alarm-button':
                item_name = self.panel_config['cards'][self.current_page].get('item_icon2', '')
                item = self.items.return_item(item_name)
                if item is not None:
                    value = not item()
                    self.logger.debug(f"item={item.id()} will be set to new value={value}")
                    item(value, self.get_shortname())
                    self.GeneratePage(self.current_page)

            else:
                entity = self.getEntityByName(pageName)
                # Handle different types
                # popupLight - popupShutter - popupThermo
                if entity['type'][:5] == 'popup':
                    popup_type = entity['type']
                    heading = entity['displayNameEntity']
                    iconId = Icons.GetIcon(entity.get('iconId', ''))
                    self.SendToPanel(f"pageType~{popup_type}~{heading}~{entity['entity']}~{iconId}")
                    # popupTimer appears without interaction
                # button / light / switch / text / etc.
                else:
                    item_name = entity['item']
                    item = self.items.return_item(item_name)
                    if item is not None:
                        if entity['type'] == 'text':
                            self.logger.debug(f"item={item.id()} will get no update because it's text")
                        elif entity['type'] == 'preset':
                            # Force update of item
                            item(entity.get('value', ''), self.get_shortname())
                        elif entity['type'] == 'input_sel':
                            # Force update of item
                            item(item(), self.get_shortname())
                        else:
                            value = item()
                            value = entity.get('offValue', 0) if value else entity.get('onValue', 1)
                            self.logger.debug(f"item={item.id()} will be set to new value={value}")
                            item(value, self.get_shortname())

                        # perhaps a complete reload with self.GeneratePage(self.current_page) is necessary in other cases
                        if self.panel_config['cards'][self.current_page]['pageType'] == 'cardMedia':
                            self.GeneratePage(self.current_page)
                        else:
                            # Reload Page with new item value
                            self.SendToPanel(self.GeneratePageElements(self.current_page))

        elif buttonAction == 'tempUpd':
            value = int(words[4]) / 10
            page_content = self.panel_config['cards'][self.current_page]
            tempitem = page_content.get('item_temp_set', 'undefined')
            self.items.return_item(tempitem)(value)
            self.GeneratePage(self.current_page)

        elif buttonAction == 'hvac_action':
            value = int(words[4])
            if value < 99:
                page_content = self.panel_config['cards'][self.current_page]
                hvacitem = page_content.get('item_mode', 'undefined')
                self.items.return_item(hvacitem)(value)
            else:
                self.logger.debug("no valid hvac action")
            self.GeneratePage(self.current_page)

        # Moving shutter for Up and Down moves
        elif buttonAction == 'up':
            # shutter moving until upper position
            entity = self.getEntityByName(pageName)
            value = entity.get('upValue', 0)
            item_name = entity['item']
            item = self.items.return_item(item_name)

            if item is not None:
                self.logger.debug(f"item={item.id()} will be set to new value={value}")
                item(value, self.get_shortname())

        elif buttonAction == 'down':
            # shutter moving down until down position
            entity = self.getEntityByName(pageName)
            value = entity.get('downValue', 1)
            item_name = entity['item']
            item = self.items.return_item(item_name)

            if item is not None:
                self.logger.debug(f"item={item.id()} will be set to new value={value}")
                item(value, self.get_shortname())

        elif buttonAction == 'stop':
            # shutter stops
            value = 1
            entity = self.getEntityByName(pageName)
            item_name = entity['item_stop']
            item = self.items.return_item(item_name)

            if item is not None:
                self.logger.debug(f"item={item.id()} will be set to new value={value}")
                item(value, self.get_shortname())

        elif buttonAction[:10] == 'alarm-mode':
            entities = self.panel_config['cards'][self.current_page]['entities']
            self.logger.debug(f"Button {buttonAction} pressed")
            password = words[4]

            if len(buttonAction) > 10:
                setNewMode = False
                anyItemTrue = False
                navigateTo = False
                for idx, entity in enumerate(entities):
                    storedPassword = entity.get('password', '')
                    page = entity.get('page', None)
                    if idx == int(buttonAction[10:]) and page:
                        navigateTo = True
                        if password.isdigit():
                            password = int(password)
                        if password == storedPassword:
                            self.current_page = self.getPageByName(page)
                            self.logger.debug("Password correct")
                            self.GeneratePage(self.current_page)
                        else:
                            self.logger.debug("Password incorrect")
                        break
                    else:
                        item = self.items.return_item(entity.get('item'))

                        if item is not None and item():
                            anyItemTrue = True
                            if storedPassword is None or storedPassword == '':
                                setNewMode = True
                            else:
                                self.logger.debug(f"Passwort needed to unlock")
                                if password.isdigit():
                                    password = int(password)
                                if password == storedPassword:
                                    self.logger.debug(f"Password correct")
                                    setNewMode = True
                                else:
                                    self.logger.debug("Password incorrect")

                if (setNewMode or not anyItemTrue) and not navigateTo:
                    for idx, entity in enumerate(entities):
                        if idx == int(buttonAction[10:]):
                            value = True
                        else:
                            value = False
                        self.items.return_item(entity.get('item'))(value)
            else:
                self.logger.warning(f"buttonAction: {buttonAction} too short")

        elif buttonAction == 'timer-start':
            parameter = words[4]
            self.logger.debug(f"timer-start called with pageName={pageName} and parameter={parameter}")
            timer = parameter.split(':')
            seconds = (int(timer[0]) * 60 + int(timer[1])) * 60 + int(timer[2]) + 1
            entity = self.getEntityByName(pageName)
            item = self.items.return_item(entity.get('item', None))
            if item is not None:
                self.logger.debug(f"item={item.id()} will be set to value={seconds - 1}")
                item(seconds, self.get_shortname())

        elif buttonAction[:6] == 'timer-':
            self.logger.debug(f"timer custom command to be implemented")

        elif buttonAction == 'mode-preset_modes':
            action = buttonAction[5:]  # unused
            parameter = words[4]
            self.logger.debug(
                f"mode-preset_modes called with pageName={pageName}, action={action} and parameter={parameter}")
            entity = self.getEntityByName(pageName)
            preset_modes = entity['preset_modes']
            item_name = entity['item_preset']
            item = self.items.return_item(item_name)
            value = str(preset_modes[int(parameter)])
            item(value, self.get_shortname())
            self.SendToPanel(self.GenerateDetailFan(pageName))

        elif buttonAction[:5] == 'mode-':
            action = buttonAction[5:]  # unused
            parameter = words[4]
            self.logger.debug(f"mode called with pageName={pageName}, action={action} and parameter={parameter}")
            entity = self.getEntityByName(pageName)
            options = entity['options']
            option_list = options.split("?")
            item_name = entity['item']
            item = self.items.return_item(item_name)
            value = str(option_list[int(parameter)])
            item(value, self.get_shortname())
            self.GeneratePage(self.current_page)

        elif buttonAction[:6] == 'media-':
            action = buttonAction[6:]
            self.logger.debug(f"media called with pageName={pageName} and action={action}")
            page_content = self.panel_config['cards'][self.current_page]
            if action == "OnOff":
                item_OnOff = self.items.return_item(page_content.get('item_OnOff', 'undefined'))
                value = not item_OnOff()
                item_OnOff(value, self.get_shortname())
            elif action == "pause":
                item_play = self.items.return_item(page_content.get('item_play', 'undefined'))
                item_pause = self.items.return_item(page_content.get('item_pause', 'undefined'))
                if item_play is not None and item_pause is not None:
                    if item_pause():
                        item_pause(False, self.get_shortname())
                    elif item_play():
                        item_pause(True, self.get_shortname())
                    else:
                        item_play(True, self.get_shortname())
            elif action == "back":
                item_back = self.items.return_item(page_content.get('item_back', 'undefined'))
                if item_back is not None:
                    item_back(True, self.get_shortname())
            elif action == "next":
                item_next = self.items.return_item(page_content.get('item_next', 'undefined'))
                if item_next is not None:
                    item_next(True, self.get_shortname())
            elif action == "shuffle":
                item_shuffle = self.items.return_item(page_content.get('item_shuffle', 'undefined'))
                if item_shuffle is not None:
                    value = not item_shuffle()
                    item_shuffle(value, self.get_shortname())
            else:
                self.logger.warning(f"Command to be implemented")

            self.GeneratePage(self.current_page)

        elif buttonAction == 'volumeSlider':
            parameter = words[4]
            self.logger.debug(f"volumeSlider called with pageName={pageName} and parameter={parameter}")
            page_content = self.panel_config['cards'][self.current_page]
            item_volume = self.items.return_item(page_content.get('item_volume', 'undefined'))
            if item_volume is not None:
                if int(words[4]) == 65535:
                    self.logger.info("volumeSlider underflow setting parameter to 0 - redraw page")
                    self.GeneratePage(self.current_page)
                else:
                    item_volume(parameter, self.get_shortname())

        elif buttonAction == 'notifyAction':
            parameter = words[4]
            self.logger.debug(f"notifyAction called with pageName={pageName} and parameter={parameter}")
            if pageName == 'driverUpdate':
                if parameter == 'yes':
                    self.update_berry_driver(self.desired_berry_driver_url)
                else:
                    self.SendToPanel('exitPopup')
            elif pageName == 'displayUpdate':
                if parameter == 'yes':
                    self.update_display_firmware(self.desired_display_firmware_url)
                else:
                    self.SendToPanel('exitPopup')
            else:
                self.logger.warning(f"notifyAction to be implemented")

        elif buttonAction == 'swipeLeft':
            self.logger.debug(f"swipedLeft to be implemented")

        elif buttonAction == 'swipeRight':
            self.logger.debug(f"swipedRight to be implemented")

        elif buttonAction == 'swipeDown':
            self.logger.debug(f"swipedDown to be implemented")

        elif buttonAction == 'swipeUp':
            self.logger.debug(f"swipedUp to be implemented")

        else:
            self.logger.debug(f"Button {buttonAction} is not declared")

    def GeneratePopupNotify(self, content) -> list:
        self.logger.debug(f"GeneratePopupNotify called with content={content}")
        # TODO split colors for different elements?
        color = rgb_dec565(getattr(Colors, self.defaultColor))

        entity = content.get('entity', '')
        heading = content.get('heading', '')
        text = content.get('text', '')
        buttonLeft = content.get('buttonLeft', '')
        buttonRight = content.get('buttonRight', '')
        timeout = content.get('timeout', 120)
        size = content.get('size', 0)
        icon = Icons.GetIcon(content.get('icon', ''))
        iconColor = rgb_dec565(getattr(Colors, content.get('iconColor', 'White')))
        out_msgs = list()
        out_msgs.append('pageType~popupNotify')
        out_msgs.append(
            f"entityUpdateDetail~{entity}~{heading}~{color}~{buttonLeft}~{color}~{buttonRight}~{color}~{text}~{color}~{timeout}~{size}~{icon}~{iconColor}")
        return out_msgs

    def GeneratePage(self, page):

        self.logger.debug(f"GeneratePage called with page={page}")

        self.panel_status['screensaver_active'] = False
        self._set_item_value('item_screensaver_active', self.panel_status['screensaver_active'])
        page_content = self.panel_config['cards'][page]

        if page_content['pageType'] == 'cardEntities':
            self.SendToPanel(self.GenerateEntitiesPage(page))

        elif page_content['pageType'] == 'cardThermo':
            self.SendToPanel(self.GenerateThermoPage(page))

        elif page_content['pageType'] == 'cardGrid':
            self.SendToPanel(self.GenerateGridPage(page))

        elif page_content['pageType'] == 'cardMedia':
            self.SendToPanel(self.GenerateMediaPage(page))

        elif page_content['pageType'] == 'cardAlarm' or page_content['pageType'] == 'cardUnlock':
            self.SendToPanel(self.GenerateAlarmPage(page))

        elif page_content['pageType'] == 'cardQR':
            self.SendToPanel(self.GenerateQRPage(page))

        elif page_content['pageType'] == 'cardPower':
            self.SendToPanel(self.GeneratePowerPage(page))

        elif page_content['pageType'] == 'cardChart' or page_content['pageType'] == 'cardLChart':
            self.SendToPanel(self.GenerateChartPage(page))

    def GenerateDetailPage(self, page, entity: str):
        self.logger.debug(f"GenerateDetailPage called with page={page} entity={entity}")
        if page == 'popupLight':
            self.SendToPanel(self.GenerateDetailLight(entity))
        elif page == 'popupShutter':
            self.SendToPanel(self.GenerateDetailShutter(entity))
        elif page == 'popupThermo':
            self.SendToPanel(self.GenerateDetailThermo(entity))
        elif page == 'popupInSel':
            self.SendToPanel(self.GenerateDetailInSel(entity))
        elif page == 'popupTimer':
            self.SendToPanel(self.GenerateDetailTimer(entity))
        elif page == 'popupFan':
            self.SendToPanel(self.GenerateDetailFan(entity))
        else:
            self.logger.warning(f"unknown detail page {page}")

    def GenerateEntitiesPage(self, page) -> list:
        self.logger.debug(f"GenerateEntitiesPage called with page={page}")
        out_msgs = list()
        out_msgs.append('pageType~cardEntities')
        out_msgs.append(self.GeneratePageElements(page))
        return out_msgs

    def GenerateGridPage(self, page) -> list:
        self.logger.debug(f"GenerateGridPage called with page={page}")
        out_msgs = list()
        out_msgs.append('pageType~cardGrid')
        out_msgs.append(self.GeneratePageElements(page))
        return out_msgs

    def GenerateThermoPage(self, page) -> list:
        self.logger.debug(f"GenerateThermoPage called with page={page}")

        out_msgs = list()
        out_msgs.append('pageType~cardThermo')

        page_content = self.panel_config['cards'][page]

        # Compile PageData according to:
        # entityUpd~*heading*~*navigation*~*item*~*currentTemp*~*destTemp*~*status*~*minTemp*~*maxTemp*~*stepTemp*[[~*iconId*~*activeColor*~*state*~*hvac_action*]]~tCurTempLbl~tStateLbl~tALbl~iconTemperature~dstTempTwoTempMode~btDetail
        # [[]] are not part of the command~ this part repeats 8 times for the buttons

        entity = page_content.get('entity', 'undefined')
        heading = page_content.get('heading', 'undefined')
        currentTemp = str(self.items.return_item(page_content.get('item_temp_current', 'undefined'))()).replace(".", ",")
        destTemp = int(self.items.return_item(page_content.get('item_temp_set', 'undefined'))() * 10)
        statusStr = 'MANU'
        minTemp = int(page_content.get('minSetValue', 5) * 10)
        maxTemp = int(page_content.get('maxSetValue', 30) * 10)
        stepTemp = int(page_content.get('stepSetValue', 0.5) * 10)
        icon_res = ''

        mode = self.items.return_item(page_content.get('item_mode', None))()
        if mode is not None:
            mode = mode if (0 < mode < 5) else 1
            modes = {1: ('Komfort', Icons.GetIcon('alpha-a-circle'), (rgb_dec565(Colors.On), 33840, 33840, 33840),
                         (1, 0, 0, 0)),
                     2: ('Standby', Icons.GetIcon('power-standby'), (33840, rgb_dec565(Colors.On), 33840, 33840),
                         (0, 1, 0, 1)),
                     3: ('Nacht', Icons.GetIcon('weather-night'), (33840, 33840, rgb_dec565(Colors.On), 33840),
                         (0, 0, 1, 0)),
                     4: ('Frost', Icons.GetIcon('head-snowflake'), (33840, 33840, 33840, rgb_dec565(Colors.On)),
                         (0, 0, 0, 1)),
                     }

            statusStr = modes[mode][0]
            (activeColor_comfort, activeColor_standby, activeColor_night, activeColor_frost) = modes[mode][2]
            (state_comfort, state_standby, state_night, state_frost) = modes[mode][3]

            bt0 = " ~0~0~99~"
            bt1 = " ~0~0~99~"
            bt2 = f"{modes[1][1]}~{activeColor_comfort}~{state_comfort}~1~"
            bt3 = f"{modes[2][1]}~{activeColor_standby}~{state_standby}~2~"
            bt4 = f"{modes[3][1]}~{activeColor_night}~{state_night}~3~"
            bt5 = f"{modes[4][1]}~{activeColor_frost}~{state_frost}~4~"
            bt6 = " ~0~0~99~"
            bt7 = " ~0~0~99~"

            icon_res = bt0 + bt1 + bt2 + bt3 + bt4 + bt5 + bt6 + bt7

        thermoPopup = '' if page_content.get('popupThermoMode1', False) else 1

        PageData = (
            'entityUpd~'
            f'{heading}~'
            f'{self.GetNavigationString(page)}~'
            f'{entity}~'
            f'{currentTemp} {self.temperatureUnit}~'  # Ist-Temperatur (String)
            f'{destTemp}~'  # Soll-Temperatur (numerisch ohne Komma in Zehntelgrad)
            f'{statusStr}~'  # Mode
            f'{minTemp}~'  # Thermostat Min-Temperatur (numerisch ohne Komma in Zehntelgrad)
            f'{maxTemp}~'  # Thermostat Max-Temperatur (numerisch ohne Komma in Zehntelgrad)
            f'{stepTemp}~'  # Schritte für Soll (0.5°C) (numerisch ohne Komma in Zehntelgrad)
            f'{icon_res}'  # Icons Status
            f'Aktuell:~'  # Todo #f'{self._get_locale("thermostat", "Currently")}~'   # Bezeichner vor aktueller Raumtemperatur
            f'Zustand:~'  # Todo #f'{self._get_locale("thermostat", "State")}~'       # Bezeichner vor State
            f"~"  # tALbl ?
            f'{self.temperatureUnit}~'  # iconTemperature dstTempTwoTempMode
            f'~'  # dstTempTwoTempMode --> Wenn Wert, dann 2 Temperaturen
            f'{thermoPopup}'  # PopUp
        )

        out_msgs.append(PageData)

        return out_msgs

    def GenerateMediaPage(self, page) -> list:
        self.logger.debug(f"GenerateMediaPage called with page={page}")
        page_content = self.panel_config['cards'][page]
        heading = page_content.get('heading', 'undefined')
        entity = page_content.get('entity', 'undefined')
        title = self.items.return_item(page_content.get('item_title', 'undefined'))
        if title is not None:
            title = title()
        titleColor = page_content.get('titleColor', 65535)
        author = self.items.return_item(page_content.get('item_author', 'undefined'))
        if author is not None:
            author = author()
        authorColor = page_content.get('authorColor', 65535)
        volume = self.items.return_item(page_content.get('item_volume', 'undefined'))
        if volume is not None:
            volume = volume()
        playPauseIcon = Icons.GetIcon('play-pause')
        onOff = page_content.get('onOffBtn', '')
        if onOff == '':
            onOffBtn = 'disable'
        elif onOff == 0:
            onOffBtn = rgb_dec565(getattr(Colors, 'White'))
        else:
            onOffBtn = rgb_dec565(getattr(Colors, 'On'))
        shuffle_item = self.items.return_item(page_content.get('iconShuffle', None))
        if shuffle_item is None or shuffle_item() == '':
            iconShuffle = 'disable'
        elif shuffle == 0:
            iconShuffle = Icons.GetIcon('shuffle-disabled')
        else:
            iconShuffle = Icons.GetIcon('shuffle')

        out_msgs = list()
        out_msgs.append('pageType~cardMedia')

        # entityUpd~Kitchen~button~navigation.up~U~65535~~~delete~~~~~~media_player.kitchen~I'm a Hurricane~~Wellmess~~100~A~64704~B~media_pl~media_player.kitchen~C~17299~Kitchen~
        PageData = (
            'entityUpd~'
            f'{heading}~'  # Heading
            f'{self.GetNavigationString(page)}~'
            f'{entity}~'
            f'{title}~'
            f'{titleColor}~'
            f'{author}~'
            f'{authorColor}~'
            f'{volume}~'
            f'{playPauseIcon}~'
            f'{onOffBtn}~'
            f'{iconShuffle}'
        )

        # TODO could be merged with GeneratePageElements?
        entities = page_content.get('entities', '{}')

        maxPresets = 6
        if len(entities) > maxPresets:
            self.logger.warning(
                f"Page definition contains too many Entities. Max allowed Entities for page={page_content['pageType']} is {maxPresets}")

        for idx, entity in enumerate(entities):
            self.logger.debug(f"entity={entity}")
            if idx > maxPresets:
                break

            name = entity.get('entity', '')
            button = entity.get('type', 'delete')
            displayNameEntity = entity.get('displayNameEntity', 'Auswahl')
            if button == 'delete':
                icon = ''
                name = ''
            else:
                icon = Icons.GetIcon(entity.get('icon', ''))
            PageData = (
                f'{PageData}~'
                f"{button}~{name}~{icon}~65535~{displayNameEntity}~ignore"
            )

        out_msgs.append(PageData)
        return out_msgs

    def GenerateAlarmPage(self, page) -> list:
        self.logger.debug(f"GenerateAlarmPage called with page={page}")

        out_msgs = list()
        out_msgs.append('pageType~cardAlarm')

        page_content = self.panel_config['cards'][page]
        # default values
        title = page_content.get('title', 'undefined')
        cardEntity = page_content.get('entity', 'undefined')
        arm = ['', '', '', '']
        iconId = Icons.GetIcon(page_content.get('icon', 'home'))
        iconColor = rgb_dec565(getattr(Colors, page_content.get('color', 'White')))
        numpadStatus = 'enable'
        flashing = 'disable'
        icon2 = page_content.get('icon2', '')
        if icon2 != '':
            icon2 = Icons.GetIcon(icon2)
        icon2Color = rgb_dec565(getattr(Colors, page_content.get('icon2Color', self.defaultColor)))
        item_icon2 = page_content.get('item_icon2', '')
        if item_icon2 != '':
            item2Action = self.items.return_item(item_icon2)
            if item2Action():
                icon2Color = rgb_dec565(getattr(Colors, page_content.get('icon2OnColor', self.defaultOnColor)))
            else:
                icon2Color = rgb_dec565(getattr(Colors, page_content.get('icon2OffColor', self.defaultOffColor)))
            # replace item with command name
            item_icon2 = 'alarm-button'

        maxEntities = 4
        for idx, entity in enumerate(page_content.get('entities', {})):
            if idx >= maxEntities:
                break
            item = self.items.return_item(entity.get('item', None))
            if item is not None and item():  # mode active
                iconId = Icons.GetIcon(entity.get('icon', 'home'))
                iconColor = rgb_dec565(getattr(Colors, entity.get('color', 'White')))
                password = entity.get('password', '')
                numpadStatus = 'disable' if password is None or password == '' else 'enable'
                flashing = 'disable' if entity.get('flashing', '') == '' else 'enable'
                arm[idx] = ""
                title = entity.get('entity', None)
            else:
                arm[idx] = entity.get('entity', None)

        # entityUpd~*entity*~*navigation*~*arm1*~*arm1ActionName*~*arm2*~*arm2ActionName*~*arm3*~*arm3ActionName*~*arm4*~*arm4ActionName*~*icon*~*iconColor*~*numpadStatus*~*flashing*
        pageData = (
            'entityUpd~'
            f'{title}~'
            f'{self.GetNavigationString(page)}~'
            f'{cardEntity}~'
            f'{arm[0]}~'  # name for mode 0
            f'alarm-mode0~'
            f'{arm[1]}~'  # name for mode 1
            f'alarm-mode1~'
            f'{arm[2]}~'  # name for mode 2
            f'alarm-mode2~'
            f'{arm[3]}~'  # name for mode 3
            f'alarm-mode3~'
            f'{iconId}~'  # iconId for active mode
            f'{iconColor}~'
            f'{numpadStatus}~'  # Numpad on/off
            f'{flashing}~'  # IconFlashing
            f'{icon2}~'
            f'{icon2Color}~'
            f'{item_icon2}'
        )
        out_msgs.append(pageData)

        return out_msgs

    def GenerateQRPage(self, page) -> list:
        self.logger.debug(f"GenerateQRPage called with page={page}")

        out_msgs = list()
        out_msgs.append('pageType~cardQR')

        page_content = self.panel_config['cards'][page]
        heading = page_content.get('heading', 'Default')
        SSID = self.items.return_item(page_content.get('item_SSID', 'undefined'))()
        Password = self.items.return_item(page_content.get('item_Password', 'undefined'))()
        hiddenPWD = page_content.get('hidePassword', False)
        iconColor = rgb_dec565(getattr(Colors, page_content.get('iconColor', 'White')))

        type1 = 'text'
        internalName1 = 'S'  # wird nicht angezeigt
        iconId1 = Icons.GetIcon('wifi')
        displayName1 = 'SSID:'
        type2 = 'text'
        internalName2 = 'P'  # wird nicht angezeigt
        iconId2 = Icons.GetIcon('key')
        displayName2 = 'Passwort:'

        if hiddenPWD:
            type2 = 'disable'
            iconId2 = ''
            displayName2 = ''

        textQR = f"WIFI:S:{SSID};T:WPA;P:{Password};;"

        # Generata PageDate according to: entityUpd, heading, navigation, textQR[, type, internalName, iconId, displayName, optionalValue]x2
        pageData = (
            'entityUpd~'  # entityUpd
            f'{heading}~'  # heading
            f'{self.GetNavigationString(page)}~'  # navigation
            f'{textQR}~'  # textQR
            f'{type1}~'  # type
            f'{internalName1}~'  # internalName
            f'{iconId1}~'  # iconId
            f'{iconColor}~'  # iconColor
            f'{displayName1}~'  # displayName
            f'{SSID}~'  # SSID
            f'{type2}~'  # type
            f'{internalName2}~'  # internalName
            f'{iconId2}~'  # iconId
            f'{iconColor}~'  # iconColor
            f'{displayName2}~'  # displayName
            f'{Password}'  # Password
        )
        out_msgs.append(pageData)

        return out_msgs

    def GeneratePowerPage(self, page) -> list:
        self.logger.debug(f"GeneratePowerPage called with page={page}")
        page_content = self.panel_config['cards'][page]

        maxItems = 6

        if len(page_content['entities']) > maxItems:
            self.logger.warning(
                f"Page definition contains too many Entities. Max allowed entities for page={page_content['pageType']} is {maxItems}")

        out_msgs = list()
        out_msgs.append('pageType~cardPower')

        textHomeBelow = self.items.return_item(page_content['itemHomeBelow'])()
        textHomeAbove = self.items.return_item(page_content['itemHomeAbove'])()
        iconHome = Icons.GetIcon(page_content.get('iconHome', 'home'))
        colorHome = rgb_dec565(getattr(Colors, page_content.get('colorHome', 'home')))

        # Generata PageDate according to: entityUpd~PowerTest~x~navUp~A~65535~~~delete~~~~~~text~sensor.power_consumption~B~17299~Power consumption~100W~1~text~sensor.power_consumption~C~17299~Power consumption~100W~1~text~sensor.today_energy~D~17299~Total energy 1~5836.0kWh~0~delete~~~~~~0~text~sensor.today_energy~E~17299~Total energy 1~5836.0kWh~-30~delete~~~~~~0~text~sensor.today_energy~F~65504~Total energy 1~5836.0kWh~90~text~sensor.today_energy~G~17299~Total energy 1~5836.0kWh~10
        pageData = (
            f"entityUpd~"
            f"{page_content['heading']}~"
            f"{self.GetNavigationString(page)}~"
            f"~"  # ignored
            f"~"  # ignored
            f"{iconHome}~"
            f"{colorHome}~"
            f"-~"  # ignored
            f"{textHomeBelow}~"
            f"-~"  # ignored
            f"-~"  # ignored
            f"-~"  # ignored
            f"-~"  # ignored
            f"-~"  # ignored
            f"-~"  # ignored
            f"{textHomeAbove}~"
            f"-~"  # ignored
        )

        for idx, entity in enumerate(page_content['entities']):
            self.logger.debug(f"entity={entity}")
            if idx > maxItems:
                break

            item = entity.get('item', '')
            value = ''
            if item != '':
                value = self.items.return_item(item)()

            name = entity.get('displayNameEntity', '')

            icon = Icons.GetIcon(entity.get('icon', ''))
            iconColor = rgb_dec565(getattr(Colors, entity.get('color', self.defaultColor)))
            speed = entity.get('speed', '')
            pageData = (
                f"{pageData}"
                f"-~"  # ignored
                f"-~"  # ignored
                f"{icon}~"
                f"{iconColor}~"
                f"{name}~"
                f"{value}~"
                f"{speed}~"
            )

        out_msgs.append(pageData)

        return out_msgs

    def GenerateChartPage(self, page) -> list:
        self.logger.debug(f"GenerateChartPage called with page={page}")
        page_content = self.panel_config['cards'][page]

        out_msgs = list()
        out_msgs.append(f"pageType~{page_content['pageType']}")

        series_list = list(self.items.return_item(page_content['item'])())

        maxElements = 88
        nr_of_xAxis_labels = 6
        nr_of_elements = len(series_list)

        if nr_of_elements > maxElements:
            self.logger.warning(
                f"Item contains too many list elements. Max allowed number of list elements for page={page_content['pageType']} is {maxElements}")
            del series_list[maxElements:]
            nr_of_elements = maxElements

        stepwidth_xAxis = round(nr_of_elements / (nr_of_xAxis_labels - 1))

        heading = page_content.get('heading', 'Chart')
        color = rgb_dec565(getattr(Colors, page_content.get('Color', self.defaultColor)))
        yAxisLabel = page_content.get('yAxisLabel', '')
        yAxisTick = '5:10'

        # Generata PageDate according to: entityUpd~heading~navigation~color~yAxisLabel~yAxisTick:[yAxisTick]*[~value[:xAxisLabel]?]*
        pageData = (
            f"entityUpd~"
            f"{heading}~"
            f"{self.GetNavigationString(page)}~"
            f"{color}~"
            f"{yAxisLabel}~"
            f"{yAxisTick}"
        )

        # Check if list is empty
        if series_list:
            max_value = max(map(lambda x: x[1], series_list))

            for idx, element in enumerate(series_list):
                value = element[1]
                if value < 0:
                    self.logger.warning(f"page={page_content['pageType']} does actually not support negativ values")
                value = round(value / max_value * 10)

                xAxisLabel = ""
                if idx % stepwidth_xAxis == 0 or idx == (nr_of_elements - 1):
                    timestamp = int(element[0] / 1000)
                    date_time = datetime.fromtimestamp(timestamp)
                    xAxisLabel = "^" + date_time.strftime("%H:%M")
                pageData = f"{pageData}~{value}{xAxisLabel}"

        out_msgs.append(pageData)

        return out_msgs

    def GeneratePageElements(self, page) -> str:
        self.logger.debug(f"GeneratePageElements called with page={page}")

        iconMapping = {"battery": "battery-outline",
                       "blinds": "blinds-open",
                       "blinds-horizontal-closed": "blinds-horizontal",
                       "blinds-vertical-closed": "blinds-vertical",
                       "curtains-closed": "curtains",
                       "circle-slice-8": "checkbox-blank-circle",
                       "door-closed": "door-open",
                       "door-sliding": "door-sliding-open",
                       "garage": "garage-open",
                       "garage-variant": "garage-open-variant",
                       "gate": "gate-open",
                       "lock": "lock-open",
                       "mailbox-up": "mailbox-outline",
                       "roller-shade-closed": "roller-shade",
                       "umbrella-closed": "umbrella",
                       "umbrella-closed-outline": "umbrella-outline",
                       "window-shutter": "window-shutter-open",
                       "window-closed": "window-open",
                       "window-closed-variant": "window-open-variant",
                       }

        page_content = self.panel_config['cards'][page]

        if page_content['pageType'] in ['cardThermo', 'cardAlarm', 'cardMedia', 'cardQR', 'cardPower', 'cardChart']:
            maxItems = 1
        elif page_content['pageType'] == 'cardEntities':
            maxItems = 6 if self.panel_model == 'us-p' else 4
        elif page_content['pageType'] == 'cardGrid':
            maxItems = 6
        else:
            maxItems = 1

        if len(page_content['entities']) > maxItems:
            self.logger.warning(
                f"Page definition contains too many Entities. Max allowed entities for page={page_content['pageType']} is {maxItems}")

        pageData = (
            f"entityUpd~"
            f"{page_content['heading']}~"
            f"{self.GetNavigationString(page)}"
        )

        for idx, entity in enumerate(page_content['entities']):
            self.logger.debug(f"entity={entity}")
            if idx > maxItems:
                break

            item = self.items.return_item(entity.get('item', None))
            value = item() if item else entity.get('optionalValue', 0)
            if entity['type'] in ['switch', 'light']:
                value = int(value)

            iconName = entity.get('iconId', '')
            status = self.items.return_item(entity.get('item_status', None))
            if (status is not None) and (iconName in iconMapping.keys()):
                if not status():
                    iconName = iconMapping[iconName]

            iconid = Icons.GetIcon(iconName)
            iconColor = entity.get('iconColor', self.defaultColor)
            if page_content['pageType'] == 'cardGrid':
                if entity['type'] == 'text':
                    iconid = str(value)[:4]  # max 4 characters
                elif value:
                    iconColor = entity.get('onColor', self.defaultOnColor)
                else:
                    iconColor = entity.get('offColor', self.defaultOffColor)

            elif page_content['pageType'] == 'cardEntities':
                if entity['type'] == 'number':
                    min_value = entity.get('min_value', 0)
                    max_value = entity.get('max_value', 100)
                    value = f"{value}|{min_value}|{max_value}"
                elif entity['type'] == 'button':
                    value = entity.get('optionalValue', 'Press')

            displayNameEntity = entity.get('displayNameEntity', '')

            iconColor = rgb_dec565(getattr(Colors, str(iconColor)))
            pageData = (
                f"{pageData}~"
                f"{entity['type']}~"
                f"{entity['entity']}~"
                f"{iconid}~"
                f"{iconColor}~"
                f"{displayNameEntity}~"
                f"{value}"
            )

        return pageData

    def GenerateDetailLight(self, pagename) -> list:
        self.logger.debug(f"GenerateDetailLight called with entity={pagename}")
        entity = self.getEntityByName(pagename)
        icon_color = rgb_dec565(getattr(Colors, self.defaultColor))
        # switch
        item = self.items.return_item(entity.get('item', ''))
        if item is None:
            switch_val = 0
        else:
            switch_val = 1 if item() else 0
        # brightness
        item_brightness = self.items.return_item(entity.get('item_brightness', None))
        if item_brightness is None:
            brightness = "disable"
        else:
            brightness = scale(item_brightness(),
                               (entity.get('min_brightness', "0"), entity.get('max_brightness', "100")), (0, 100))
        # temperature
        item_temperature = self.items.return_item(entity.get('item_temperature', None))
        if item_temperature is None:
            temperature = "disable"
        else:
            temperature = scale(item_temperature(),
                                (entity.get('min_temperature', "0"), entity.get('max_temperature', "100")), (100, 0))
        # color
        item_color = self.items.return_item(entity.get('item_color', None))
        if item_color is None:
            color = "disable"
        else:
            color = 0
        # effect?
        effect_supported = entity.get('effect_supported', "disable")
        # labels TODO translate
        color_translation = "Farbe"
        brightness_translation = "Helligkeit"
        color_temp_translation = "Farbtemperatur"

        out_msgs = list()
        out_msgs.append(
            f"entityUpdateDetail~{entity['entity']}~~{icon_color}~{switch_val}~{brightness}~{temperature}~{color}~{color_translation}~{color_temp_translation}~{brightness_translation}~{effect_supported}")
        return out_msgs

    def GenerateDetailShutter(self, pagename) -> list:
        self.logger.debug(f"GenerateDetailShutter called with entity={pagename} to be implemented")
        entity = self.getEntityByName(pagename)
        # iconId = entity.get('iconId', '') # not used
        itemname_pos = entity.get('item_pos', None)
        item_pos = self.items.return_item(itemname_pos)
        if item_pos is not None:
            sliderPos = scale(item_pos(),
                              (entity.get('min_pos', 0), entity.get('max_pos', 100)), (0, 100))
            textPosition = entity.get('textPosition', 'Position')
        else:
            sliderPos = 'disable'
            textPosition = ''
        secondrow = entity.get('secondrow', 'Zweite Reihe')
        icon1 = ''  # leave empty
        iconUp = 2
        iconStop = 3
        iconDown = 4
        iconUpStatus = 5
        iconStopStatus = 6
        iconDownStatus = 7
        iconTiltLeft = 8
        iconTiltStop = 9
        iconTiltRight = 10
        iconTiltLeftStatus = 11
        iconTiltStopStatus = 12
        iconTiltRightStatus = 13
        itemname_tilt = entity.get('item_tilt', None)
        item_tilt = self.items.return_item(itemname_tilt)
        if item_tilt is not None:
            textTilt = entity.get('textTilt', 'Lamellen')
            tiltPos = scale(item_tilt(),
                            (entity.get('min_tilt', 0), entity.get('max_tilt', 100)), (0, 100))
        else:
            textTilt = ''
            tiltPos = 'disable'
        out_msgs = list()
        out_msgs.append(
            f"entityUpdateDetail~{pagename}~{sliderPos}~{secondrow}~{textPosition}~{icon1}~{iconUp}~{iconStop}~{iconDown}~{iconUpStatus}~{iconStopStatus}~{iconDownStatus}~{textTilt}~{iconTiltLeft}~{iconTiltStop}~{iconTiltRight}~{iconTiltLeftStatus}~{iconTiltStopStatus}~{iconTiltRightStatus}~{tiltPos}")
        return out_msgs

    def GenerateDetailThermo(self, pagename) -> list:
        self.logger.debug(f"GenerateDetailThermo called with entity={pagename} to be implemented")
        icon_id = 1
        icon_color = 65535
        heading = "Detail Thermo"
        mode = 'Mode'
        out_msgs = list()
        out_msgs.append(
            f"entityUpdateDetail~{pagename}~{icon_id}~{icon_color}~{heading}~{mode}~mode1~mode1?mode2?mode3~{heading}~{mode}~mode1~mode1?mode2?mode3~{heading}~{mode}~mode1~mode1?mode2?mode3~")
        return out_msgs

    def GenerateDetailInSel(self, pagename) -> list:
        self.logger.debug(f"GenerateDetailInSel called with entity={pagename}")
        entity = self.getEntityByName(pagename)
        # iconId = entity.get('iconId', '') # not used
        iconColor = entity.get('iconColor', 'White')
        modeType = ''  # not used
        state = ''
        itemName = entity.get('item', None)
        item = self.items.return_item(itemName)
        if item is not None:
            state = item()
            if state == '':
                state = 'empty'
            self.logger.debug(f"item={item} itemValue={state}")
        options = entity.get('options', '')

        iconColor = rgb_dec565(getattr(Colors, iconColor))
        out_msgs = list()
        out_msgs.append(f"entityUpdateDetail2~{pagename}~~{iconColor}~{modeType}~{state}~{options}")
        return out_msgs

    def GenerateDetailTimer(self, pagename) -> list:
        self.logger.debug(f"GenerateDetailTimer called with entity={pagename}")
        entity = self.getEntityByName(pagename)
        editable = entity.get('editable', 1)
        actionleft = entity.get('actionleft', '')
        actioncenter = entity.get('actioncenter', '')
        actionright = entity.get('actionright', '')
        buttonleft = entity.get('buttonleft', '')  # pause
        buttoncenter = entity.get('buttoncenter', '')  # cancel
        buttonright = entity.get('buttonright', '')  # finish
        item = self.items.return_item(entity.get('item', None))
        value = 0
        if item is not None:
            value = item()

        seconds = item() % 60
        minutes = int((value - seconds) / 60)
        out_msgs = list()
        # first entity is used to identify the correct page, the second is used for the button event
        out_msgs.append(
            f"entityUpdateDetail~{pagename}~~65535~{pagename}~{minutes}~{seconds}~{editable}~{actionleft}~{actioncenter}~{actionright}~{buttonleft}~{buttoncenter}~{buttonright}")
        return out_msgs

    def GenerateDetailFan(self, pagename) -> list:
        self.logger.debug(f"GenerateDetailFan called with entity={pagename}")
        entity = self.getEntityByName(pagename)
        item = self.items.return_item(entity.get('item', None))
        switch_val = 1 if item() else 0
        icon_color = entity.get('color', 65535)
        item_speed = self.items.return_item(entity.get('item_speed', None))
        speed = item_speed()
        percentage_step = entity.get("percentage_step", 25)
        speedMax = 100
        if percentage_step is None:
            speed = "disable"
        else:
            if speed is None:
                speed = 0
            speed = round(speed / percentage_step)
            speedMax = int(100 / percentage_step)

        speed_translation = "Geschwindigkeit"

        item_preset = self.items.return_item(entity.get('item_preset', None))
        preset_mode = item_preset()
        preset_modes = entity.get("preset_modes", [])
        if preset_modes is not None:
            preset_modes = "?".join(preset_modes)
        else:
            preset_modes = ""

        out_msgs = list()
        out_msgs.append(
            f"entityUpdateDetail~{pagename}~~{icon_color}~{switch_val}~{speed}~{speedMax}~{speed_translation}~{preset_mode}~{preset_modes}")
        return out_msgs

    def SendToPanel(self, payload):
        self.logger.debug(f"SendToPanel called with payload={payload}")

        if self.lastPayload == payload:
            self.logger.error("SendToPanel: duplicate payload no transfer")
        else:
            if isinstance(payload, list):
                for idx, entry in enumerate(payload):
                    if idx >= len(self.lastPayload) or self.lastPayload[idx] != payload[idx]:
                        self.publish_tasmota_topic(payload=entry)
                    else:
                        self.logger.debug("SendToPanel: identical element in payload")
                self.lastPayload = payload
            else:
                self.lastPayload = [payload]
                self.publish_tasmota_topic(payload=payload)

    def GetNavigationString(self, page) -> str:
        """
        Determination of page navigation (CustomSend - Payload)
        """

        self.logger.debug(f"GetNavigationString called with page={page}")

        # left navigation arrow | right navigation arrow
        # X | X
        # 0 = no arrow
        # 1 | 1 = right and left navigation arrow
        # 2 | 0 = (right) up navigation arrow
        # 2 | 2 = (right) up navigation arrow | (left) home navigation icon

        # ToDo: Handling of SubPages
        # if (activePage.subPage):
        #     return '2|2'

        iconleft = Icons.GetIcon('arrow-left-bold')
        iconright = Icons.GetIcon('arrow-right-bold')
        iconup = Icons.GetIcon('arrow-up-bold')
        iconhome = Icons.GetIcon('home')
        iconreload = Icons.GetIcon('reload')

        if page == 0:
            left = f"bHome~{iconreload}"
            right = f"bNext~{iconright}"
        elif page == self.no_of_cards - 1:
            left = f"bPrev~{iconleft}"
            right = f"bHome~{iconhome}"
        elif page == -1 or page == -2:
            left = f"bUp~{iconup}"
            right = f"bHome~{iconhome}"
        else:
            left = f"bPrev~{iconleft}"
            right = f"bNext~{iconright}"

        return f"button~{left}~65535~~~button~{right}~65535~~"

    def update_berry_driver(self, url):
        self.logger.info('update_berry_driver running')
        self.publish_tasmota_topic("cmnd", self.tasmota_topic, "Backlog", f"UpdateDriverVersion {url}; Restart 1")

    def update_display_firmware(self, url):
        self.logger.info('update_display_firmware called')
        self.publish_tasmota_topic("cmnd", self.tasmota_topic, "FlashNextion", url)

    ################################################################
    #  Properties
    ################################################################

    @property
    def no_of_cards(self):
        cards = self.panel_config.get('cards')
        if cards:
            return len(cards)

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

    def send_mqtt_from_nspanel(self, msg):

        self.logger.debug(f"send_mqtt_from_nspanel called with msg={msg}")

        link = {1: [f'tele/{self.tasmota_topic}/RESULT', '{"CustomRecv": "event,startup,45,eu"}'],
                2: [f'tele/{self.tasmota_topic}/RESULT',
                    '{"CustomRecv": "event,buttonPress2,licht.eg.tv_wand_nische,OnOff,0"}'],
                3: [f'tele/{self.tasmota_topic}/RESULT',
                    '{"CustomRecv": "event,buttonPress2,licht.eg.tv_wand_nische,OnOff,1"}'],
                4: [f'tele/{self.tasmota_topic}/RESULT', '{"CustomRecv": "event,sleepReached,cardEntities"}'],
                5: [f'tele/{self.tasmota_topic}/RESULT', '{"CustomRecv": "event,buttonPress2,screensaver,bExit,1"}'],
                6: [f'tele/{self.tasmota_topic}/SENSOR',
                    '{"Time":"2022-12-03T13:11:26","ANALOG":{"Temperature1":26.9},"ESP32":{"Temperature":36.7},"TempUnit":"C"}'],
                7: [f'tele/{self.tasmota_topic}/STATE',
                    '{"Time":"2022-12-03T13:11:26","Uptime":"0T00:20:13","UptimeSec":1213,"Heap":126,"SleepMode":"Dynamic","Sleep":0,"LoadAvg":1000,"MqttCount":1,"Berry":{"HeapUsed":14,"Objects":218},"POWER1":"ON","POWER2":"OFF","Wifi":{"AP":1,"SSId":"FritzBox","BSSId":"F0:B0:14:4A:08:CD","Channel":1,"Mode":"11n","RSSI":42,"Signal":-79,"LinkCount":1,"Downtime":"0T00:00:07"}}'],
                20: [f'tele/{self.tasmota_topic}/LWT', 'Online'],
                21: [f'tele/{self.tasmota_topic}/LWT', 'Offline'],
                }

        if isinstance(msg, int):
            if msg not in link:
                return "Message No not defined"

            topic = link.get(msg, [f'tele/{self.tasmota_topic}/RESULT', ''])[0]
            payload = link.get(msg, ['', ''])[1]

        elif isinstance(msg, dict):
            topic = f'tele/{self.tasmota_topic}/RESULT'
            payload = msg

        else:
            return f"Message not defined"

        try:
            self.publish_topic(topic=topic, payload=payload, retain=False)
        except Exception as e:
            return f"Exception during send_mqtt_from_nspanel: {e}"
        else:
            return f"send_mqtt_from_nspanel with payload={payload} to topic={topic} done"


def rgb_dec565(rgb):
    return ((rgb['red'] >> 3) << 11) | (rgb['green'] >> 2) << 5 | ((rgb['blue']) >> 3)


def scale(val, src, dst):
    """
    Scale the given value from the scale of src to the scale of dst.
    """
    return int(((val - src[0]) / (src[1] - src[0])) * (dst[1] - dst[0]) + dst[0])


def hsv2rgb(h, s, v):
    rgb = colorsys.hsv_to_rgb(h, s, v)
    return tuple(round(i * 255) for i in rgb)


def pos_to_color(x, y, wh):
    # r = 160/2
    r = wh / 2
    x = round((x - r) / r * 100) / 100
    y = round((r - y) / r * 100) / 100

    r = math.sqrt(x * x + y * y)
    if r > 1:
        sat = 0
    else:
        sat = r
    hsv = (math.degrees(math.atan2(y, x)) % 360 / 360, sat, 1)
    rgb = hsv2rgb(hsv[0], hsv[1], hsv[2])
    return rgb


def getWeatherCondition(weatherid, day: bool = True):
    """Get weather condition from weather data."""
    condition_classes = {
        'cloudy': [803, 804],
        'fog': [701, 721, 741],
        'hail': [906],
        'lightning': [210, 211, 212, 221],
        'lightning_rainy': [200, 201, 202, 230, 231, 232],
        'partlycloudy': [801, 802],
        'pouring': [504, 314, 502, 503, 522],
        'rainy': [300, 301, 302, 310, 311, 312, 313, 500, 501, 520, 521],
        'snowy': [600, 601, 602, 611, 612, 620, 621, 622],
        'snowy_rainy': [511, 615, 616],
        'windy': [905, 951, 952, 953, 954, 955, 956, 957],
        'windy_variant': [958, 959, 960, 961],
        'exceptional': [711, 731, 751, 761, 762, 771, 900, 901, 962, 903, 904],
    }
    if weatherid == 800:  # same code for day and night
        if day:
            return ['sunny']
        else:
            return ['clear_night']
    else:
        return [k for k, v in condition_classes.items() if weatherid in v]

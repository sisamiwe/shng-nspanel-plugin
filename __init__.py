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

from datetime import datetime, timedelta
import time
import yaml
import queue
import os
import sys
import colorsys
import math

from lib.model.mqttplugin import MqttPlugin
from .webif import WebInterface

from . import nspanel_icons_colors
from lib.item import Items

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

        self.items = Items.get_instance()

        # get the parameters for the plugin (as defined in metadata plugin.yaml):
        try:
            self.webif_pagelength = self.get_parameter_value('webif_pagelength')
            self.tasmota_topic = self.get_parameter_value('topic')
            self.telemetry_period = self.get_parameter_value('telemetry_period')
            self.config_file_location = self.get_parameter_value('config_file_location')
            self.full_topic = self.get_parameter_value('full_topic').lower()
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
        self.panel_status = {'online': False, 'online_timeout': datetime.now(), 'uptime': '-', 'sensors': {}, 'relay': {}}
        self.custom_msg_queue = queue.Queue(maxsize=50)  # Queue containing last 50 messages containing "CustomRecv"
        self.panel_items = {}
        self.panel_config_items = []
        self.panel_config_items_page = {}
        self.panel_version = 0
        self.panel_model = ''
        self.useMediaEvents = False
        self.screensaverEnabled = False
        self.alive = None

        # read panel config file
        try:
            self.panel_config = self._parse_config_file()
        except Exception as e:
            self.logger.warning(f"Exception during parsing of page config yaml file occurred: {e}")
            self._init_complete = False
            return

        # link items from config to method 'update_item'
        self._get_items_of_panel_config_to_update_item()

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
                        self.publish_tasmota_topic('cmnd', self.tasmota_topic, f"POWER{relay}", value, item, bool_values=['OFF', 'ON'])

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
            elif not self.screensaverEnabled:
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

        self.logger.debug('Add scheduler for weather')
        dt = dt + timedelta(seconds=20)
        self.scheduler_add('update_weather', self.HandleScreensaverWeatherUpdate, next=dt, cycle=3600)

    def _remove_scheduler(self):
        """
        remove scheduler for cyclic time and date update
        """

        self.logger.debug('Remove scheduler for cyclic updates of time and date')

        self.scheduler_remove('update_time')
        self.scheduler_remove('update_date')

        self.logger.debug('Remove scheduler for online status')
        self.scheduler_remove('check_online_status')
        
        self.logger.debug('Remove scheduler for weather')
        self.scheduler_remove('update_weather')

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

    def _get_items_of_panel_config_to_update_item(self):
        """
        Put all item out of config file to update_item
        """

        for idx, card in enumerate(self.panel_config['cards']):
            self.panel_config_items_page[idx] = []
            temp = []
            entities = card.get('entities')
            if entities is not None:
                for entity in entities:
                    item = entity.get('item')
                    # Add all possible items without check, parse_item is only called for valid items
                    if item is not None and item not in temp:
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
        return self.locale.get(group, {}).get(entry, {}).get(
            self.panel_config.get('config', {}).get('locale', 'de-DE'))

    def send_current_time(self):
        secondLine = self.panel_config.get('config', {}).get('screensaver_secondLine', '')
        self.publish_tasmota_topic(payload=f"time~{time.strftime('%H:%M', time.localtime())}~{secondLine}")

    def send_current_date(self):
        self.publish_tasmota_topic(payload=f"date~{time.strftime('%A, %d.%B %Y', time.localtime())}")

    def send_screensavertimeout(self):
        screensavertimeout = self.panel_config.get('config', {}).get('screensaver_timeout', 10)
        self.publish_tasmota_topic(payload=f"timeout~{screensavertimeout}")

    def send_panel_brightness(self):
        brightness_screensaver = self.panel_config.get('config', {}).get('brightness_screensaver', 10)
        brightness_active = self.panel_config.get('config', {}).get('brightness_active', 100)
        self.publish_tasmota_topic(payload=f"dimmode~{brightness_screensaver}~{brightness_active}~6371")

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
                self.screensaverEnabled = True
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
                # event,pageOpenDetail,popupLight,entity
                self.screensaverEnabled = False
                self.GenerateDetailPage(words[2], words[3])

            elif method == 'buttonPress2':
                self.screensaverEnabled = False
                self.logger.debug(f"{words[0]} - {words[1]} - {words[2]} - {words[3]}")
                self.HandleButtonEvent(words)

            elif method == 'button1':
                self.screensaverEnabled = False
                self.HandleHardwareButton(method)

            elif method == 'button2':
                self.screensaverEnabled = False
                self.HandleHardwareButton(method)

    def HandleStartupProcess(self):
        self.logger.debug("HandleStartupProcess called")
        self.send_current_time()
        self.send_current_date()
        self.send_screensavertimeout()
        self.send_panel_brightness()

    def HandleScreensaver(self):
        self.publish_tasmota_topic(payload="pageType~screensaver")
        self.HandleScreensaverIconUpdate()
        #self.HandleScreensaverWeatherUpdate()  # Geht nur wenn NOTIFY leer wäre! Wird in Nextion so geregelt.       
        self.HandleScreensaverColors()  # Geht nur wenn NOTIFY leer wäre! Wird in Nextion so geregelt.

    def HandleScreensaverColors(self):
        # payload: color~background~time~timeAMPM~date~tMainIcon~tMainText~tForecast1~tForecast2~tForecast3~tForecast4~tF1Icon~tF2Icon~tF3Icon~tF4Icon~tForecast1Val~tForecast2Val~tForecast3Val~tForecast4Val~bar~tMRIcon~tMR~tTimeAdd
        self.logger.info('Function HandleScreensaverColors to be done')
        background = rgb_dec565(getattr(Colors, 'HMIDark'))
        timestr = rgb_dec565(getattr(Colors, 'White'))
        timeAPPM = rgb_dec565(getattr(Colors, 'White'))
        date = rgb_dec565(getattr(Colors, 'White'))
        tMainIcon = rgb_dec565(getattr(Colors, 'White'))
        tMainText = rgb_dec565(getattr(Colors, 'White'))
        tForecast1 = rgb_dec565(getattr(Colors, 'White'))
        tForecast2 = rgb_dec565(getattr(Colors, 'White'))
        tForecast3 = rgb_dec565(getattr(Colors, 'White'))
        tForecast4 = rgb_dec565(getattr(Colors, 'White'))
        tF1Icon = rgb_dec565(getattr(Colors, 'White'))
        tF2Icon = rgb_dec565(getattr(Colors, 'White'))
        tF3Icon = rgb_dec565(getattr(Colors, 'White'))
        tF4Icon = rgb_dec565(getattr(Colors, 'White'))
        tForecast1Val = rgb_dec565(getattr(Colors, 'White'))
        tForecast2Val = rgb_dec565(getattr(Colors, 'White'))
        tForecast3Val = rgb_dec565(getattr(Colors, 'White'))
        tForecast4Val = rgb_dec565(getattr(Colors, 'White'))
        bar = rgb_dec565(getattr(Colors, 'White'))
        tMRIcon = rgb_dec565(getattr(Colors, 'White'))
        tMR = rgb_dec565(getattr(Colors, 'White'))
        tTimeAdd = rgb_dec565(getattr(Colors, 'White'))
        self.publish_tasmota_topic(
            payload=f"color~{background}~{timestr}~{timeAPPM}~{date}~{tMainIcon}~{tMainText}~{tForecast1}~{tForecast2}~{tForecast3}~{tForecast4}~{tF1Icon}~{tF2Icon}~{tF3Icon}~{tF4Icon}~{tForecast1Val}~{tForecast2Val}~{tForecast3Val}~{tForecast4Val}~{bar}~{tMRIcon}~{tMR}~{tTimeAdd}")

    def HandleScreensaverIconUpdate(self):
        self.logger.info('Function HandleScreensaverIconUpdate to be implemented')
        icon1 = Icons.GetIcon('wifi')
        icon2 = Icons.GetIcon('wifi-alert')
        icon1Color = rgb_dec565(getattr(Colors, 'White'))
        icon2Color = rgb_dec565(getattr(Colors, 'White'))
        icon1Font = 1
        icon2Font = 1
        self.publish_tasmota_topic(
            payload=f"statusUpdate~{icon1}~{icon1Color}~{icon2}~{icon2Color}~{icon1Font}~{icon2Font}")    

    def HandleScreensaverWeatherUpdate(self):
        self.logger.info('Function HandleScreensaverWeatherUpdate')
        weather = self.panel_config.get('config', {}).get('weather', {})

        if weather:
            # actual weather
            tMainIcon = Icons.GetIcon(self.items.return_item(weather[0].get('icon'))())
            tMainText = self.items.return_item(weather[0].get('text'))()
            optionalLayoutIcon = ""
            optionalLayoutText = ""
            if weather[0].get('alternativeLayout', False):
                optionalLayoutItemValue = self.items.return_item(weather[0].get('second_icon'))()
                optionalLayoutIcon = Icons.GetIcon(optionalLayoutItemValue)
                if not optionalLayoutIcon:
                    optionalLayoutIcon = optionalLayoutItemValue
                optionalLayoutIcon = optionalLayoutIcon
                optionalLayoutText = self.items.return_item(weather[0].get('second_text'))()

            # forecast day 1
            tForecast1 = self.items.return_item(weather[1].get('day'))()
            tF1Icon = Icons.GetIcon(self.items.return_item(weather[1].get('icon'))())
            tForecast1Val = self.items.return_item(weather[1].get('text'))()

            # forecast day 2
            tForecast2 = self.items.return_item(weather[2].get('day'))()
            tF2Icon = Icons.GetIcon(self.items.return_item(weather[2].get('icon'))())
            tForecast2Val = self.items.return_item(weather[2].get('text'))()

            # forecast day 3
            tForecast3 = self.items.return_item(weather[3].get('day'))()
            tF3Icon = Icons.GetIcon(self.items.return_item(weather[3].get('icon'))())
            tForecast3Val = self.items.return_item(weather[3].get('text'))()

            # forecast day 4
            tForecast4 = self.items.return_item(weather[4].get('day'))()
            tF4Icon = Icons.GetIcon(self.items.return_item(weather[4].get('icon'))())
            tForecast4Val = self.items.return_item(weather[4].get('text'))()

            payload = f"weatherUpdate~{tMainIcon}~{tMainText}~{tForecast1}~{tF1Icon}~{tForecast1Val}~{tForecast2}~{tF2Icon}~{tForecast2Val}~{tForecast3}~{tF3Icon}~{tForecast3Val}~{tForecast4}~{tF4Icon}~{tForecast4Val}~{optionalLayoutIcon}~{optionalLayoutText}"
            self.publish_tasmota_topic(payload=payload)

    def GenerateScreensaverNotify(self, value) -> list:
        self.logger.debug(f"GenerateScreensaverNotify called with item={value}")

        if not self.screensaverEnabled:
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

    def HandleButtonEvent(self, words):

        # words=['event', 'buttonPress2', 'licht.eg.tv_wand_nische', 'OnOff', '1']

        pageName = words[2]
        buttonAction = words[3]

        self.logger.debug(
            f"HandleButtonEvent: {words[0]} - {words[1]} - {words[2]} - {words[3]} - current_page={self.current_page}")

        if 'navigate' in pageName:
            self.GeneratePage(pageName[8:len(pageName)])

        if buttonAction == 'bExit':
            self.GeneratePage(self.current_page)

        elif buttonAction == 'OnOff':
            # TODO don't use direct items
            value = int(words[4])
            item = self.items.return_item(pageName)
            if item is not None:
                self.logger.debug(f"item={item.id()} will be set to new value={value}")
                item(value, self.get_shortname())
            # get item from entity
            else:
                entities = self.panel_config['cards'][self.current_page]['entities']
                entity = next((entity for entity in entities if entity["entity"] == pageName), None)
                item_name = entity.get('item_onoff', '')
                item = self.items.return_item(item_name)
                if item is not None:
                    value = entity.get('onValue', 1) if value else entity.get('offValue', 0)
                    self.logger.debug(f"item={item.id()} will be set to new value={value}")
                    item(value, self.get_shortname())

        elif buttonAction == 'brightnessSlider':
            value = int(words[4])
            self.logger.debug(f"brightnessSlider called with pageName={pageName}")
            entities = self.panel_config['cards'][self.current_page]['entities']
            entity = next((entity for entity in entities if entity["entity"] == pageName), None)
            item = self.items.return_item(entity.get('item_brightness', None))
            scaled_value = scale(value, (0, 100),
                                 (entity.get('min_brightness', "0"), entity.get('max_brightness', "100")))
            if item is not None:
                self.logger.debug(f"item={item.id()} will be set to new scaled_value={scaled_value}")
                item(scaled_value, self.get_shortname())

        elif buttonAction == 'colorTempSlider':
            value = int(words[4])
            self.logger.debug(f"colorTempSlider called with pageName={pageName}")
            entities = self.panel_config['cards'][self.current_page]['entities']
            entity = next((entity for entity in entities if entity["entity"] == pageName), None)
            item = self.items.return_item(entity.get('item_temperature', None))
            scaled_value = scale(value, (100, 0),
                                 (entity.get('min_temperature', "0"), entity.get('max_temperature', "100")))
            if item is not None:
                self.logger.debug(f"item={item.id()} will be set to new scaled_value={scaled_value}")
                item(scaled_value, self.get_shortname())

        elif buttonAction == 'colorWheel':
            value = words[4]
            self.logger.debug(f"colorWheel called with pageName={pageName}")
            entities = self.panel_config['cards'][self.current_page]['entities']
            entity = next((entity for entity in entities if entity["entity"] == pageName), None)
            item = self.items.return_item(entity.get('item_color', None))
            value = value.split('|')
            rgb = pos_to_color(int(value[0]), int(value[1]), int(value[2]))
            red = rgb[0]
            blue = rgb[1]
            green = rgb[2]
            if item is not None:
                item(f"[{red}, {blue}, {green}]", self.get_shortname())
                self.logger.debug(f"item={item.id()} will be set to red={red} blue={blue} green={green}")

        elif buttonAction == 'button':
            self.logger.debug(f"button called with pageName={pageName}")
            searching = words[2]
            if searching == 'bHome':
                self.current_page = 0
                self.GeneratePage(self.current_page)

            elif searching == 'bNext':
                self._next_page()
                self.GeneratePage(self.current_page)

            elif searching == 'bPrev':
                self._previous_page()
                self.GeneratePage(self.current_page)

            else:
                entities = self.panel_config['cards'][self.current_page]['entities']
                entity = next((entity for entity in entities if entity["entity"] == searching), None)
                # Handle different types
                # popupLight - popupShutter - popupThermo
                if entity['type'][:5] == 'popup':
                    popup_type = entity['type']
                    heading = entity['displayNameEntity']
                    iconId = Icons.GetIcon(entity['iconId'])
                    self.SendToPanel(f"pageType~{popup_type}~{heading}~{entity['entity']}~{iconId}")
                    # popupTimer appears without interaction
                # button / light / switch / text / etc.
                else:
                    item_name = entity['item']
                    item = self.items.return_item(item_name)
                    if item is not None:
                        if entity['type'] == 'text':
                            self.logger.debug(f"item={item.id()} will get no update because it's text")
                        else:
                            value = item()
                            value = entity.get('offValue', 0) if value else entity.get('onValue', 1)
                            self.logger.debug(f"item={item.id()} will be set to new value={value}")
                            item(value, self.get_shortname())

        elif buttonAction == 'tempUpd':
            value = int(words[4]) / 10
            page_content = self.panel_config['cards'][self.current_page]
            tempitem = page_content.get('items', 'undefined')
            self.items.return_item(tempitem.get('item_temp_set', None))(value)
            self.GeneratePage(self.current_page)

        elif buttonAction == 'hvac_action':
            value = int(words[4])
            if value < 99:
                page_content = self.panel_config['cards'][self.current_page]
                hvacitem = page_content.get('items', 'undefined')
                self.items.return_item(hvacitem.get('item_mode', None))(value)
            else:
                self.logger.debug("no valid hvac action")
            self.GeneratePage(self.current_page)

        # Moving shutter for Up and Down moves
        elif buttonAction == 'up':
            # shutter moving until upper position
            value = 0
            item = self.items.return_item(words[2])

            if item is not None:
                self.logger.debug(f"item={item.id()} will be set to new value={value}")
                item(value, self.get_shortname())
            self.GeneratePage(self.current_page)

        elif buttonAction == 'down':
            # shutter moving down until down position
            value = 255
            item = self.items.return_item(words[2])
            self.logger.debug(item)

            if item is not None:
                self.logger.debug(f"item={item.id()} will be set to new value={value}")
                item(value, self.get_shortname())
            self.GeneratePage(self.current_page)

        elif buttonAction == 'stop':
            # shutter stops
            value = 1
            item = self.items.return_item(
                "EG.Arbeiten.Rollladen.stop")  # Das ITEM muss noch mit Config verknüpft werden

            if item is not None:
                self.logger.debug(f"item={item.id()} will be set to new value={value}")
                item(value, self.get_shortname())
            self.GeneratePage(self.current_page)

        # Alarmpage Button Handle
        elif buttonAction == 'Alarm.Modus1':  # Anwesend
            self.logger.debug(f"Button {buttonAction} pressed")

            password = words[4]

            page_content = self.panel_config['cards'][self.current_page]
            items = page_content.get('items', 'undefined')
            pwd = items.get('Password', None)
            item1 = self.items.return_item(items.get('arm1ActionName'))
            item2 = self.items.return_item(items.get('arm2ActionName'))
            item3 = self.items.return_item(items.get('arm3ActionName'))
            item4 = self.items.return_item(items.get('arm4ActionName'))

            if item3():
                self.logger.debug("Passwort needed to unlock")
                if password == pwd:
                    self.logger.debug(f"Password {password} = {pwd} correct")
                    self.items.return_item(items.get('arm1ActionName', None))(True)
                    self.items.return_item(items.get('arm2ActionName', None))(False)
                    self.items.return_item(items.get('arm3ActionName', None))(False)
                    self.items.return_item(items.get('arm4ActionName', None))(False)
                else:
                    self.logger.debug("Password incorrect")
            else:
                self.items.return_item(items.get('arm1ActionName', None))(True)
                self.items.return_item(items.get('arm2ActionName', None))(False)
                self.items.return_item(items.get('arm3ActionName', None))(False)
                self.items.return_item(items.get('arm4ActionName', None))(False)

            self.GeneratePage(self.current_page)

        elif buttonAction == 'Alarm.Modus2':  # Abwesend
            self.logger.debug(f"Button {buttonAction} pressed")
            password = words[4]

            page_content = self.panel_config['cards'][self.current_page]
            items = page_content.get('items', 'undefined')
            pwd = items.get('Password', None)
            item1 = self.items.return_item(items.get('arm1ActionName'))
            item2 = self.items.return_item(items.get('arm2ActionName'))
            item3 = self.items.return_item(items.get('arm3ActionName'))
            item4 = self.items.return_item(items.get('arm4ActionName'))

            if item3():
                self.logger.debug("Passwort needed to unlock")
                if password == pwd:
                    self.logger.debug(f"Password {password} = {pwd} correct")
                    self.items.return_item(items.get('arm1ActionName', None))(False)
                    self.items.return_item(items.get('arm2ActionName', None))(True)
                    self.items.return_item(items.get('arm3ActionName', None))(False)
                    self.items.return_item(items.get('arm4ActionName', None))(False)
                else:
                    self.logger.debug("Password incorrect")
            else:
                self.items.return_item(items.get('arm1ActionName', None))(False)
                self.items.return_item(items.get('arm2ActionName', None))(True)
                self.items.return_item(items.get('arm3ActionName', None))(False)
                self.items.return_item(items.get('arm4ActionName', None))(False)

            self.GeneratePage(self.current_page)

        elif buttonAction == 'Alarm.Modus3':  # Urlaub
            self.logger.debug(f"Button {buttonAction} pressed")

            password = words[4]

            page_content = self.panel_config['cards'][self.current_page]
            items = page_content.get('items', 'undefined')
            pwd = items.get('Password', None)
            item1 = self.items.return_item(items.get('arm1ActionName'))
            item2 = self.items.return_item(items.get('arm2ActionName'))
            item3 = self.items.return_item(items.get('arm3ActionName'))
            item4 = self.items.return_item(items.get('arm4ActionName'))

            if item3():
                self.logger.debug("Passwort needed to unlock")
                if password == pwd:
                    self.logger.debug(f"Password {password} = {pwd} correct")
                    self.items.return_item(items.get('arm1ActionName', None))(False)
                    self.items.return_item(items.get('arm2ActionName', None))(False)
                    self.items.return_item(items.get('arm3ActionName', None))(True)
                    self.items.return_item(items.get('arm4ActionName', None))(False)
                else:
                    self.logger.debug("Password incorrect")
            else:
                self.items.return_item(items.get('arm1ActionName', None))(False)
                self.items.return_item(items.get('arm2ActionName', None))(False)
                self.items.return_item(items.get('arm3ActionName', None))(True)
                self.items.return_item(items.get('arm4ActionName', None))(False)

            self.GeneratePage(self.current_page)

        elif buttonAction == 'Alarm.Modus4':  # Gäste
            password = words[4]

            page_content = self.panel_config['cards'][self.current_page]
            items = page_content.get('items', 'undefined')
            pwd = items.get('Password', None)
            item1 = self.items.return_item(items.get('arm1ActionName'))
            item2 = self.items.return_item(items.get('arm2ActionName'))
            item3 = self.items.return_item(items.get('arm3ActionName'))
            item4 = self.items.return_item(items.get('arm4ActionName'))

            if item3():
                self.logger.debug("Passwort needed to unlock")
                if password == pwd:
                    self.logger.debug(f"Password {password} = {pwd} correct")
                    self.items.return_item(items.get('arm1ActionName', None))(False)
                    self.items.return_item(items.get('arm2ActionName', None))(False)
                    self.items.return_item(items.get('arm3ActionName', None))(False)
                    self.items.return_item(items.get('arm4ActionName', None))(True)
                else:
                    self.logger.debug("Password incorrect")
            else:
                self.items.return_item(items.get('arm1ActionName', None))(False)
                self.items.return_item(items.get('arm2ActionName', None))(False)
                self.items.return_item(items.get('arm3ActionName', None))(False)
                self.items.return_item(items.get('arm4ActionName', None))(True)

            self.GeneratePage(self.current_page)

        elif buttonAction == 'timer-start':
            parameter = words[4]
            self.logger.debug(f"timer-start called with pageName={pageName} and parameter={parameter}")
            timer = parameter.split(':')
            seconds = (int(timer[0]) * 60 + int(timer[1])) * 60 + int(timer[2]) + 1
            entities = self.panel_config['cards'][self.current_page]['entities']
            entity = next((entity for entity in entities if entity["entity"] == pageName), None)
            item = self.items.return_item(entity.get('item', None))
            if item is not None:
                self.logger.debug(f"item={item.id()} will be set to value={seconds - 1}")
                item(seconds, self.get_shortname())

        elif buttonAction[:6] == 'timer-':
            self.logger.debug(f"timer custom command to be implemented")

        elif buttonAction[:6] == 'media-':
            action = buttonAction[6:]
            self.logger.debug(f"media called with pageName={pageName} and action={action}")
            if action == "OnOff":
                self.logger.debug(f"OnOff to be implemented")
            elif action == "pause":
                self.logger.debug(f"Pause to be implemented")
            elif action == "back":
                self.logger.debug(f"Back to be implemented")
            elif action == "next":
                self.logger.debug(f"Next to be implemented")
            elif action == "shuffle":
                self.logger.debug(f"Shuffle to be implemented")

        elif buttonAction == 'volumeSlider':
            self.logger.debug(f"volumeSlider to be implemented")

        elif buttonAction == 'swipeLeft':
            self.logger.debug(f"swipedLeft on screensaver to be implemented")

        elif buttonAction == 'swipeRight':
            self.logger.debug(f"swipedRight on screensaver to be implemented")

        elif buttonAction == 'swipeDown':
            self.logger.debug(f"swipedDown on screensaver to be implemented")

        elif buttonAction == 'swipeUp':
            self.logger.debug(f"swipedUp on screensaver to be implemented")

        else:
            self.logger.warning(f"unknown buttonAction {buttonAction}")

    def GeneratePopupNotify(self, value) -> list:
        self.logger.debug(f"GeneratePopupNotify called with item={value}")
        content = {
            'heading': "",
            'text': "",
            'buttonLeft': "",
            'buttonRight': "",
            'timeout': 120,
            'size': 0,
            'icon': "",
            'iconColor': 'White',
        }
        # TODO split colors for different elements?
        color = rgb_dec565(getattr(Colors, self.panel_config.get('defaultOnColor', "White")))

        for variable, value in value.items():
            content[variable] = value

        heading = content['heading']
        text = content['text']
        buttonLeft = content['buttonLeft']
        buttonRight = content['buttonRight']
        timeout = content['timeout']
        size = content['size']
        icon = content['icon']
        iconColor = content['iconColor']
        out_msgs = list()
        out_msgs.append('pageType~popupNotify')
        out_msgs.append(
            f"entityUpdateDetail~topic~{heading}~{color}~{buttonLeft}~{color}~{buttonRight}~{color}~{text}~{color}~{timeout}~{size}~{icon}~{iconColor}")
        return out_msgs

    def GeneratePage(self, page):

        self.logger.debug(f"GeneratePage called with page={page}")

        page_content = self.panel_config['cards'][page]

        if page_content['pageType'] == 'cardEntities':
            self.SendToPanel(self.GenerateEntitiesPage(page))

        elif page_content['pageType'] == 'cardThermo':
            self.SendToPanel(self.GenerateThermoPage(page))

        elif page_content['pageType'] == 'cardGrid':
            self.SendToPanel(self.GenerateGridPage(page))

        elif page_content['pageType'] == 'cardMedia':
            self.useMediaEvents = True
            self.SendToPanel(self.GenerateMediaPage(page))

        elif page_content['pageType'] == 'cardAlarm':
            self.SendToPanel(self.GenerateAlarmPage(page))

        elif page_content['pageType'] == 'cardQR':
            self.SendToPanel(self.GenerateQRPage(page))

        elif page_content['pageType'] == 'cardPower':
            self.SendToPanel(self.GeneratePowerPage(page))

        elif page_content['pageType'] == 'cardChart':
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

        temperatureUnit = self.panel_config.get('config', {}).get('temperatureUnit', '°C')

        out_msgs = list()
        out_msgs.append('pageType~cardThermo')

        page_content = self.panel_config['cards'][page]

        # Compile PageData according to:
        # entityUpd~*heading*~*navigation*~*item*~*currentTemp*~*destTemp*~*status*~*minTemp*~*maxTemp*~*stepTemp*[[~*iconId*~*activeColor*~*state*~*hvac_action*]]~tCurTempLbl~tStateLbl~tALbl~iconTemperature~dstTempTwoTempMode~btDetail
        # [[]] are not part of the command~ this part repeats 8 times for the buttons

        entity = page_content.get('entity', 'undefined')
        heading = page_content.get('heading', 'undefined')
        items = page_content.get('items', 'undefined')
        currentTemp = str(self.items.return_item(items.get('item_temp_current', 'undefined'))()).replace(".", ",")
        destTemp = int(self.items.return_item(items.get('item_temp_set', 'undefined'))() * 10)
        statusStr = 'MANU'
        minTemp = int(items.get('minSetValue', 5) * 10)
        maxTemp = int(items.get('maxSetValue', 30) * 10)
        stepTemp = int(items.get('stepSetValue', 0.5) * 10)
        icon_res = ''

        mode = self.items.return_item(items.get('item_mode', None))()
        if mode is not None:
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

        thermoPopup = '' if items.get('popupThermoMode1', False) else 1

        PageData = (
            'entityUpd~'
            f'{heading}~'
            f'{self.GetNavigationString(page)}~'
            f'{entity}~'
            f'{currentTemp} {temperatureUnit}~'  # Ist-Temperatur (String)
            f'{destTemp}~'  # Soll-Temperatur (numerisch ohne Komma in Zehntelgrad)
            f'{statusStr}~'  # Mode
            f'{minTemp}~'  # Thermostat Min-Temperatur (numerisch ohne Komma in Zehntelgrad)
            f'{maxTemp}~'  # Thermostat Max-Temperatur (numerisch ohne Komma in Zehntelgrad)
            f'{stepTemp}~'  # Schritte für Soll (0.5°C) (numerisch ohne Komma in Zehntelgrad)
            f'{icon_res}'  # Icons Status
            f'Aktuell:~'  # Todo #f'{self._get_locale("thermostat", "Currently")}~'   # Bezeichner vor aktueller Raumtemperatur
            f'Zustand:~'  # Todo #f'{self._get_locale("thermostat", "State")}~'       # Bezeichner vor State
            f"~"  # tALbl ?
            f'{temperatureUnit}~'  # iconTemperature dstTempTwoTempMode
            f'~'  # dstTempTwoTempMode --> Wenn Wert, dann 2 Temperaturen
            f'{thermoPopup}'  # PopUp
        )

        out_msgs.append(PageData)

        return out_msgs

    def GenerateMediaPage(self, page) -> list:
        self.logger.debug(f"GenerateMediaPage called with page={page} to be implemented")
        page_content = self.panel_config['cards'][page]
        heading = page_content.get('heading', 'undefined')
        entity = page_content.get('entity', 'undefined')
        title = page_content.get('title', 'undefined')
        titleColor = page_content.get('titleColor', 65535)
        author = page_content.get('author', 'undefined')
        authorColor = page_content.get('authorColor', 65535)
        volume = page_content.get('volume', 0)
        playPauseIcon = page_content.get('playPauseIcon', Icons.GetIcon('play-pause'))
        onOff = page_content.get('onOffBtn', 0)
        if onOff == '':
            onOffBtn = 'disable'
        elif onOff == 0:
            onOffBtn = rgb_dec565(getattr(Colors, 'White'))
        else:
            onOffBtn = rgb_dec565(getattr(Colors, 'On'))
        shuffle = page_content.get('iconShuffle', 0)
        if shuffle == '':
            iconShuffle = 'disable'
        elif shuffle == 0:
            iconShuffle = Icons.GetIcon('shuffle-disabled')
        else:
            iconShuffle = Icons.GetIcon('shuffle')
            
        dummy_items = "button~name~icon~65535~name~ignore"

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
            f'{iconShuffle}~'
            f'{dummy_items}~'
            f'{dummy_items}~'
            f'{dummy_items}~'
            f'{dummy_items}~'
            f'{dummy_items}~'
            f'{dummy_items}~'
        )

        out_msgs.append(PageData)
        return out_msgs

    def GenerateAlarmPage(self, page) -> list:
        self.logger.debug(f"GenerateAlarmPage called with page={page}")

        out_msgs = list()
        out_msgs.append('pageType~cardAlarm')

        page_content = self.panel_config['cards'][page]

        entity = page_content.get('entity', 'undefined')
        items = page_content.get('items', 'undefined')
        iconId = Icons.GetIcon('home')  # Icons.GetIcon(items.get('iconId', 'home'))
        iconColor = rgb_dec565(
            getattr(Colors, 'White'))  # rgb_dec565(getattr(Colors, items.get('iconColor', 'White')))
        arm1 = items.get('arm1', None)
        arm2 = items.get('arm2', None)
        arm3 = items.get('arm3', None)
        arm4 = items.get('arm4', None)
        arm1ActionName = items.get('arm1ActionName', None)
        arm2ActionName = items.get('arm2ActionName', None)
        arm3ActionName = items.get('arm3ActionName', None)
        arm4ActionName = items.get('arm4ActionName', None)
        numpadStatus = 'disable'  # items.get('numpadStatus', "disable")
        flashing = 'disable'  # items.get('flashing', "disable")

        item1 = self.items.return_item(arm1ActionName)
        item2 = self.items.return_item(arm2ActionName)
        item3 = self.items.return_item(arm3ActionName)
        item4 = self.items.return_item(arm4ActionName)

        if item1():  # Modus Anwesend
            iconId = Icons.GetIcon('home')
            iconColor = rgb_dec565(getattr(Colors, 'White'))
            numpadStatus = 'disable'
            flashing = 'disable'
            arm1 = ""
            arm2 = items.get('arm2', None)
            arm3 = items.get('arm3', None)
            arm4 = items.get('arm4', None)

        if item2():  # Modus Abwesend
            iconId = Icons.GetIcon('home-lock')
            iconColor = rgb_dec565(getattr(Colors, 'Yellow'))
            numpadStatus = 'disable'
            flashing = 'disable'
            arm1 = items.get('arm1', None)
            arm2 = ""
            arm3 = items.get('arm3', None)
            arm4 = items.get('arm4', None)

        if item3():  # Modus Urlaub
            iconId = Icons.GetIcon('hiking')
            iconColor = rgb_dec565(getattr(Colors, 'Red'))
            numpadStatus = 'enable'
            flashing = 'disable'
            arm1 = items.get('arm1', None)
            arm2 = items.get('arm2', None)
            arm3 = ""
            arm4 = items.get('arm4', None)

        if item4():  # Modus Gäste
            iconId = Icons.GetIcon('home')
            iconColor = rgb_dec565(getattr(Colors, 'Green'))
            numpadStatus = 'disable'
            flashing = 'enable'
            arm1 = items.get('arm1', None)
            arm2 = items.get('arm2', None)
            arm3 = items.get('arm3', None)
            arm4 = ""

        # entityUpd~*entity*~*navigation*~*arm1*~*arm1ActionName*~*arm2*~*arm2ActionName*~*arm3*~*arm3ActionName*~*arm4*~*arm4ActionName*~*icon*~*iconColor*~*numpadStatus*~*flashing*
        pageData = (
            'entityUpd~'
            f'{entity}~'
            f'{self.GetNavigationString(page)}~'
            f'{arm1}~'  # Statusname for modus 1
            f'{arm1ActionName}~'  # Status item for modus 1
            f'{arm2}~'  # Statusname for modus 2
            f'{arm2ActionName}~'  # Status item for modus 2
            f'{arm3}~'  # Statusname for modus 3
            f'{arm3ActionName}~'  # Status item for modus 3
            f'{arm4}~'  # Statusname for modus 4
            f'{arm4ActionName}~'  # Status item for modus 4
            f'{iconId}~'  # iconId for which modus activated
            f'{iconColor}~'  # iconColor
            f'{numpadStatus}~'  # Numpad on/off
            f'{flashing}'  # IconFlashing
        )
        out_msgs.append(pageData)

        return out_msgs

    def GenerateQRPage(self, page) -> list:
        self.logger.debug(f"GenerateQRPage called with page={page}")

        out_msgs = list()
        out_msgs.append('pageType~cardQR')

        page_content = self.panel_config['cards'][page]
        heading = page_content.get('heading', 'Default')
        items = page_content.get('items')
        SSID = self.items.return_item(items.get('SSID', 'undefined'))()
        Password = self.items.return_item(items.get('Password', 'undefined'))()
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

        textHome = self.items.return_item(page_content['itemHome'])()
        iconHome = Icons.GetIcon(page_content.get('iconHome', 'home'))
        colorHome = rgb_dec565(getattr(Colors, page_content.get('colorHome', 'home')))

        # Generata PageDate according to: entityUpd~heading~navigation~colorHome~iconHome~textHome[~iconColor~icon~  speed~valueDown]x6
        pageData = (
            f"entityUpd~"
            f"{page_content['heading']}~"
            f"{self.GetNavigationString(page)}~"
            f"{colorHome}~"
            f"{iconHome}~"
            f"{textHome}~"
        )

        for idx, entity in enumerate(page_content['entities']):
            self.logger.debug(f"entity={entity}")
            if idx > maxItems:
                break

            item = entity.get('item', '')
            value = ''
            if item != '':
                value = self.items.return_item(item)()

            icon = Icons.GetIcon(entity.get('icon', ''))
            iconColor = rgb_dec565(getattr(Colors, entity.get('color', self.panel_config['config']['defaultColor'])))
            speed = entity.get('speed', '')
            pageData = (
                f"{pageData}"
                f"{iconColor}~"
                f"{icon}~"
                f"{speed}~"
                f"{value}~"
            )

        out_msgs.append(pageData)

        return out_msgs

    def GenerateChartPage(self, page) -> list:
        self.logger.debug(f"GenerateChartPage called with page={page}")
        page_content = self.panel_config['cards'][page]

        out_msgs = list()
        out_msgs.append('pageType~cardChart')

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
        color = rgb_dec565(getattr(Colors, page_content.get('Color', 'Green')))
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

            item = self.items.return_item(entity['item'])
            value = item() if item else entity.get('optionalValue', 0)
            if entity['type'] in ['switch', 'light']:
                value = int(value)

            iconid = Icons.GetIcon(entity['iconId'])
            if iconid == '':
                iconid = entity['iconId']

            if page_content['pageType'] == 'cardGrid':
                if value:
                    iconColor = rgb_dec565(
                        getattr(Colors, entity.get('onColor', self.panel_config['config']['defaultOnColor'])))
                else:
                    iconColor = rgb_dec565(
                        getattr(Colors, entity.get('offColor', self.panel_config['config']['defaultOffColor'])))

            else:
                iconColor = entity.get('iconColor')

            # define displayNameEntity
            displayNameEntity = entity.get('displayNameEntity')

            # handle cardGrid with text
            if page_content['pageType'] == 'cardGrid':
                if entity['type'] == 'text':
                    iconColor = rgb_dec565(
                        getattr(Colors, entity.get('Color', self.panel_config['config']['defaultColor'])))
                    iconid = str(value)[:4]  # max 4 characters

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
        entities = self.panel_config['cards'][self.current_page]['entities']
        entity = next((entity for entity in entities if entity["entity"] == pagename), None)
        icon_color = rgb_dec565(getattr(Colors, self.panel_config.get('defaultColor', "White")))
        # switch
        item_onoff = self.items.return_item(entity.get('item_onoff', None))
        if item_onoff is None:
            switch_val = 0
        else:
            switch_val = 1 if item_onoff() else 0
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

    def GenerateDetailShutter(self, entity) -> list:
        self.logger.debug(f"GenerateDetailShutter called with entity={entity} to be implemented")
        sliderPos = 50
        secondrow = 'Zweite Reihe'
        textPosition = 'Position'
        icon1 = 1
        iconUp = 2
        iconStop = 3
        iconDown = 4
        iconUpStatus = 5
        iconStopStatus = 6
        iconDownStatus = 7
        textTilt = 'Lamellen'
        iconTiltLeft = 8
        iconTiltStop = 9
        iconTiltRight = 10
        iconTiltLeftStatus = 11
        iconTiltStopStatus = 12
        iconTiltRightStatus = 13
        tiltPos = 50
        out_msgs = list()
        out_msgs.append(
            f"entityUpdateDetail~{entity}~{sliderPos}~{secondrow}~{textPosition}~{icon1}~{iconUp}~{iconStop}~{iconDown}~{iconUpStatus}~{iconStopStatus}~{iconDownStatus}~{textTilt}~{iconTiltLeft}~{iconTiltStop}~{iconTiltRight}~{iconTiltLeftStatus}~{iconTiltStopStatus}~{iconTiltRightStatus}~{tiltPos}")
        return out_msgs

    def GenerateDetailThermo(self, entity) -> list:
        self.logger.debug(f"GenerateDetailThermo called with entity={entity} to be implemented")
        icon_id = 1
        icon_color = 65535
        heading = "Detail Thermo"
        mode = 'Mode'
        out_msgs = list()
        out_msgs.append(
            f"entityUpdateDetail~{entity}~{icon_id}~{icon_color}~{heading}~{mode}~mode1~mode1?mode2?mode3~{heading}~{mode}~mode1~mode1?mode2?mode3~{heading}~{mode}~mode1~mode1?mode2?mode3~")
        return out_msgs

    def GenerateDetailInSel(self, entity) -> list:
        self.logger.debug(f"GenerateDetailInSel called with entity={entity} to be implemented")
        icon_id = 1
        icon_color = 65535
        input_sel = 'Input Select'
        state = 'State'
        options = 'option1?option2?option3'
        out_msgs = list()
        out_msgs.append(f"entityUpdateDetail2~{entity}~{icon_id}~{icon_color}~{input_sel}~{state}~{options}")
        return out_msgs

    def GenerateDetailTimer(self, pagename) -> list:
        self.logger.debug(f"GenerateDetailTimer called with entity={pagename}")
        entities = self.panel_config['cards'][self.current_page]['entities']
        entity = next((entity for entity in entities if entity.get('entity', '') == pagename), None)
        editable = entity.get('editable', 1)
        actionleft = entity.get('actionleft', '')
        actioncenter = entity.get('actioncenter', '')
        actionright = entity.get('actionright', '')
        buttonleft = entity.get('buttonleft', '')  # pause
        buttoncenter = entity.get('buttoncenter', '')  # cancel
        buttonright = entity.get('buttonright', '')  # finish
        item = self.items.return_item(entity.get('item', None))
        if item is not None:
            value = item()
            seconds = item() % 60
            minutes = int((value - seconds) / 60)
            out_msgs = list()
            # first entity is used to identify the correct page, the second is used for the button event
            out_msgs.append(
                f"entityUpdateDetail~{entity['entity']}~~65535~{entity['entity']}~{minutes}~{seconds}~{editable}~{actionleft}~{actioncenter}~{actionright}~{buttonleft}~{buttoncenter}~{buttonright}")
            return out_msgs

    def SendToPanel(self, payload):
        self.logger.debug(f"SendToPanel called with payload={payload}")

        if isinstance(payload, list):
            for entry in payload:
                self.publish_tasmota_topic(payload=entry)
        else:
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

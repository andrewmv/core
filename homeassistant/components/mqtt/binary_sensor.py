"""Support for MQTT binary sensors."""
from __future__ import annotations

from datetime import timedelta
import functools
import logging

import voluptuous as vol

from homeassistant.components import binary_sensor
from homeassistant.components.binary_sensor import (
    DEVICE_CLASSES_SCHEMA,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_FORCE_UPDATE,
    CONF_NAME,
    CONF_PAYLOAD_OFF,
    CONF_PAYLOAD_ON,
    CONF_VALUE_TEMPLATE,
)
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import homeassistant.helpers.event as evt
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import dt as dt_util

from . import PLATFORMS, MqttValueTemplate, subscription
from .. import mqtt
from .const import CONF_ENCODING, CONF_QOS, CONF_STATE_TOPIC, DOMAIN
from .debug_info import log_messages
from .mixins import (
    MQTT_ENTITY_COMMON_SCHEMA,
    MqttAvailability,
    MqttEntity,
    async_setup_entry_helper,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "MQTT Binary sensor"
CONF_OFF_DELAY = "off_delay"
DEFAULT_PAYLOAD_OFF = "OFF"
DEFAULT_PAYLOAD_ON = "ON"
DEFAULT_FORCE_UPDATE = False
CONF_EXPIRE_AFTER = "expire_after"
PAYLOAD_NONE = "None"

PLATFORM_SCHEMA = mqtt.MQTT_RO_PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_DEVICE_CLASS): DEVICE_CLASSES_SCHEMA,
        vol.Optional(CONF_EXPIRE_AFTER): cv.positive_int,
        vol.Optional(CONF_FORCE_UPDATE, default=DEFAULT_FORCE_UPDATE): cv.boolean,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_OFF_DELAY): cv.positive_int,
        vol.Optional(CONF_PAYLOAD_OFF, default=DEFAULT_PAYLOAD_OFF): cv.string,
        vol.Optional(CONF_PAYLOAD_ON, default=DEFAULT_PAYLOAD_ON): cv.string,
    }
).extend(MQTT_ENTITY_COMMON_SCHEMA.schema)

DISCOVERY_SCHEMA = PLATFORM_SCHEMA.extend({}, extra=vol.REMOVE_EXTRA)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up MQTT binary sensor through configuration.yaml."""
    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)
    await _async_setup_entity(hass, async_add_entities, config)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MQTT binary sensor dynamically through MQTT discovery."""
    setup = functools.partial(
        _async_setup_entity, hass, async_add_entities, config_entry=config_entry
    )
    await async_setup_entry_helper(hass, binary_sensor.DOMAIN, setup, DISCOVERY_SCHEMA)


async def _async_setup_entity(
    hass, async_add_entities, config, config_entry=None, discovery_data=None
):
    """Set up the MQTT binary sensor."""
    async_add_entities([MqttBinarySensor(hass, config, config_entry, discovery_data)])


class MqttBinarySensor(MqttEntity, BinarySensorEntity):
    """Representation a binary sensor that is updated by MQTT."""

    _entity_id_format = binary_sensor.ENTITY_ID_FORMAT

    def __init__(self, hass, config, config_entry, discovery_data):
        """Initialize the MQTT binary sensor."""
        self._state = None
        self._expiration_trigger = None
        self._delay_listener = None
        expire_after = config.get(CONF_EXPIRE_AFTER)
        if expire_after is not None and expire_after > 0:
            self._expired = True
        else:
            self._expired = None

        MqttEntity.__init__(self, hass, config, config_entry, discovery_data)

    @staticmethod
    def config_schema():
        """Return the config schema."""
        return DISCOVERY_SCHEMA

    def _setup_from_config(self, config):
        self._value_template = MqttValueTemplate(
            self._config.get(CONF_VALUE_TEMPLATE),
            entity=self,
        ).async_render_with_possible_json_value

    async def _subscribe_topics(self):
        """(Re)Subscribe to topics."""

        @callback
        def off_delay_listener(now):
            """Switch device off after a delay."""
            self._delay_listener = None
            self._state = False
            self.async_write_ha_state()

        @callback
        @log_messages(self.hass, self.entity_id)
        def state_message_received(msg):
            """Handle a new received MQTT state message."""
            # auto-expire enabled?
            expire_after = self._config.get(CONF_EXPIRE_AFTER)

            if expire_after is not None and expire_after > 0:

                # When expire_after is set, and we receive a message, assume device is
                # not expired since it has to be to receive the message
                self._expired = False

                # Reset old trigger
                if self._expiration_trigger:
                    self._expiration_trigger()
                    self._expiration_trigger = None

                # Set new trigger
                expiration_at = dt_util.utcnow() + timedelta(seconds=expire_after)

                self._expiration_trigger = async_track_point_in_utc_time(
                    self.hass, self._value_is_expired, expiration_at
                )

            payload = self._value_template(msg.payload)
            if not payload.strip():  # No output from template, ignore
                _LOGGER.debug(
                    "Empty template output for entity: %s with state topic: %s. Payload: '%s', with value template '%s'",
                    self._config[CONF_NAME],
                    self._config[CONF_STATE_TOPIC],
                    msg.payload,
                    self._config.get(CONF_VALUE_TEMPLATE),
                )
                return

            if payload == self._config[CONF_PAYLOAD_ON]:
                self._state = True
            elif payload == self._config[CONF_PAYLOAD_OFF]:
                self._state = False
            elif payload == PAYLOAD_NONE:
                self._state = None
            else:  # Payload is not for this entity
                template_info = ""
                if self._config.get(CONF_VALUE_TEMPLATE) is not None:
                    template_info = f", template output: '{payload}', with value template '{str(self._config.get(CONF_VALUE_TEMPLATE))}'"
                _LOGGER.info(
                    "No matching payload found for entity: %s with state topic: %s. Payload: '%s'%s",
                    self._config[CONF_NAME],
                    self._config[CONF_STATE_TOPIC],
                    msg.payload,
                    template_info,
                )
                return

            if self._delay_listener is not None:
                self._delay_listener()
                self._delay_listener = None

            off_delay = self._config.get(CONF_OFF_DELAY)
            if self._state and off_delay is not None:
                self._delay_listener = evt.async_call_later(
                    self.hass, off_delay, off_delay_listener
                )

            self.async_write_ha_state()

        self._sub_state = await subscription.async_subscribe_topics(
            self.hass,
            self._sub_state,
            {
                "state_topic": {
                    "topic": self._config[CONF_STATE_TOPIC],
                    "msg_callback": state_message_received,
                    "qos": self._config[CONF_QOS],
                    "encoding": self._config[CONF_ENCODING] or None,
                }
            },
        )

    @callback
    def _value_is_expired(self, *_):
        """Triggered when value is expired."""
        self._expiration_trigger = None
        self._expired = True

        self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        return self._state

    @property
    def device_class(self):
        """Return the class of this sensor."""
        return self._config.get(CONF_DEVICE_CLASS)

    @property
    def force_update(self):
        """Force update."""
        return self._config[CONF_FORCE_UPDATE]

    @property
    def available(self) -> bool:
        """Return true if the device is available and value has not expired."""
        expire_after = self._config.get(CONF_EXPIRE_AFTER)
        # mypy doesn't know about fget: https://github.com/python/mypy/issues/6185
        return MqttAvailability.available.fget(self) and (  # type: ignore[attr-defined]
            expire_after is None or not self._expired
        )

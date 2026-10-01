# SPDX-License-Identifier: GPL-2.0-or-later

"""Exercise real effect routing with temporary driver files and no device bus."""

import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openrazer_daemon.device import DeviceCollection
from openrazer_daemon.dbus_services.dbus_methods import chroma_keyboard
from openrazer_daemon.hardware.device_base import RazerDevice
from openrazer_daemon.misc.effect_sync import EffectSync
from openrazer_daemon.misc.ripple_effect import RippleManager


class SimulatedDevice:

    # Use production endpoints and routing without starting D-Bus or hardware.
    send_effect_event = RazerDevice.send_effect_event
    notify_observers = RazerDevice.notify_observers
    notify = RazerDevice.notify
    register_parent = RazerDevice.register_parent
    register_observer = RazerDevice.register_observer
    remove_observer = RazerDevice.remove_observer
    disable_notify = RazerDevice.disable_notify
    effect_sync = RazerDevice.effect_sync
    _set_custom_effect = RazerDevice._set_custom_effect
    _set_key_row = RazerDevice._set_key_row
    setCustom = chroma_keyboard.set_custom_effect
    setKeyRow = chroma_keyboard.set_key_row
    setSpectrum = chroma_keyboard.set_spectrum_effect

    def __init__(self, directory):
        self.directory = directory
        self.logger = logging.getLogger(__name__)
        self._observer_list = []
        self._parent = None
        self._disable_notifications = False
        self._effect_sync_propagate_up = False
        self.key_manager = SimpleNamespace(temp_key_store_state=True)
        self.zone = {"backlight": {"effect": "ripple"}}
        for filename in ('matrix_effect_custom', 'matrix_custom_frame', 'matrix_effect_spectrum'):
            (directory / filename).write_bytes(b'unchanged')

    def get_driver_path(self, filename):
        return self.directory / filename

    def set_persistence(self, zone, key, value):
        self.zone[zone][key] = value


class CustomEffectSyncTest(unittest.TestCase):

    def make_pair(self, sync=True):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        collection = DeviceCollection()
        devices = []
        for name in ('source', 'peer'):
            path = Path(directory.name) / name
            path.mkdir()
            device = SimulatedDevice(path)
            device.effect_sync = sync
            collection.add(name, name, device)
            effect_sync = EffectSync(device, len(devices))
            self.addCleanup(effect_sync.close)
            # Replace only the worker thread; use the real RippleManager observer.
            with patch('openrazer_daemon.misc.ripple_effect.RippleEffectThread') as thread:
                thread.return_value.is_alive.return_value = False
                device.ripple = RippleManager(device, len(devices))
            self.addCleanup(device.ripple.close)
            device.observer = Mock()
            device.register_observer(device.observer)
            devices.append(device)
        return devices

    def assert_peer_untouched(self, peer):
        for filename in ('matrix_effect_custom', 'matrix_custom_frame', 'matrix_effect_spectrum'):
            self.assertEqual(peer.get_driver_path(filename).read_bytes(), b'unchanged')
        peer.observer.notify.assert_not_called()
        peer.ripple._ripple_thread.disable.assert_not_called()
        self.assertTrue(peer.key_manager.temp_key_store_state)

    def test_custom_operations_only_notify_local_observers(self):
        payload = bytes([0, 0, 0, 255, 0, 0])
        operations = (
            ('setCustom', (), 'matrix_effect_custom', b'1'),
            ('setKeyRow', (payload,), 'matrix_custom_frame', payload),
        )
        for sync in (True, False):
            for method, args, filename, expected in operations:
                with self.subTest(sync=sync, method=method):
                    source, peer = self.make_pair(sync)
                    getattr(source, method)(*args)
                    self.assertEqual(source.get_driver_path(filename).read_bytes(), expected)
                    source.observer.notify.assert_called_once_with(('effect', source, 'setCustom'))
                    source.ripple._ripple_thread.disable.assert_called_once_with()
                    self.assertFalse(source.key_manager.temp_key_store_state)
                    self.assert_peer_untouched(peer)

    def test_ordinary_effect_still_respects_sync_setting(self):
        for sync in (True, False):
            with self.subTest(sync=sync):
                source, peer = self.make_pair(sync)
                source.setSpectrum()
                self.assertEqual(source.get_driver_path('matrix_effect_spectrum').read_bytes(), b'1')
                source.observer.notify.assert_called_once_with(('effect', source, 'setSpectrum'))
                if sync:
                    self.assertEqual(peer.get_driver_path('matrix_effect_spectrum').read_bytes(), b'1')
                    peer.observer.notify.assert_called_once_with(('effect', source, 'setSpectrum'))
                    peer.ripple._ripple_thread.disable.assert_called_once_with()
                    self.assertFalse(peer.disable_notify)
                else:
                    self.assert_peer_untouched(peer)

    def test_ordinary_sync_survives_custom_updates(self):
        source, peer = self.make_pair()
        source.setKeyRow(bytes([0, 0, 0, 255, 0, 0]))
        source.setCustom()
        self.assertTrue(source.effect_sync)
        self.assertFalse(source.disable_notify)
        self.assert_peer_untouched(peer)
        source.setSpectrum()
        self.assertEqual(peer.get_driver_path('matrix_effect_spectrum').read_bytes(), b'1')
        peer.observer.notify.assert_called_once_with(('effect', source, 'setSpectrum'))

    def test_notification_suppression_preserved(self):
        for method, args in (('setCustom', ()), ('setKeyRow', (b'frame',)), ('setSpectrum', ())):
            with self.subTest(method=method):
                source, peer = self.make_pair()
                source.disable_notify = True
                getattr(source, method)(*args)
                source.observer.notify.assert_not_called()
                source.ripple._ripple_thread.disable.assert_not_called()
                self.assert_peer_untouched(peer)

    def test_private_ripple_updates_do_not_stop_animation(self):
        source, peer = self.make_pair()
        source.ripple.set_rgb_matrix(b'frame')
        source.ripple.refresh_keyboard()
        self.assertEqual(source.get_driver_path('matrix_custom_frame').read_bytes(), b'frame')
        self.assertEqual(source.get_driver_path('matrix_effect_custom').read_bytes(), b'1')
        source.observer.notify.assert_not_called()
        source.ripple._ripple_thread.disable.assert_not_called()
        self.assertTrue(source.key_manager.temp_key_store_state)
        self.assert_peer_untouched(peer)

    def test_custom_effect_without_parent_still_notifies_local_observers(self):
        source, peer = self.make_pair()
        source._parent = None
        source.setCustom()
        self.assertEqual(source.get_driver_path('matrix_effect_custom').read_bytes(), b'1')
        source.observer.notify.assert_called_once_with(('effect', source, 'setCustom'))
        self.assert_peer_untouched(peer)

    def test_other_notification_shapes_still_propagate(self):
        # The generic notification contract allows tuples shorter than effect events.
        for message in (('other',), ('effect',), ('other', None, 'setCustom')):
            with self.subTest(message=message):
                device = SimpleNamespace(
                    logger=Mock(), _disable_notifications=False,
                    _effect_sync_propagate_up=True, _parent=Mock(),
                    _observer_list=[Mock()],
                )
                RazerDevice.notify_observers(device, message)
                device._parent.notify_parent.assert_called_once_with(message)
                device._observer_list[0].notify.assert_called_once_with(message)

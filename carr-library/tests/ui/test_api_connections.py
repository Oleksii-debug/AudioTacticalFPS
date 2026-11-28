from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from carr_library.providers import ProviderProfile, ProviderRequest
from carr_library.ui.api_connections import ConnectionsController, parse_budget


class LocalTransport:
    def __init__(self):
        self.called = 0

    def post(self, profile, route, body, headers, **kwargs):
        self.called += 1
        return b'{"choices":[{"message":{"content":"Local generated text"}}]}'


class ConnectionControllerTests(unittest.TestCase):
    def test_decimal_budget_is_exact(self):
        self.assertEqual(parse_budget('0.012345'), 12345)
        for value in ('NaN', 'Infinity', '-1', '10001', '0.0000001', 'not money'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_budget(value)

    def test_profile_does_not_enable_remote_calls_and_output_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'private'
            root.mkdir()
            transport = LocalTransport()
            controller = ConnectionsController(root, transport=transport)
            profile = ProviderProfile('local', 'Local model', 'openai-compatible',
                'http://127.0.0.1:8080/v1', 'local-model', ('llm',), local=True)
            controller.store.save(profile)
            self.assertEqual(transport.called, 0)
            request = ProviderRequest('llm', 'Synthetic task')
            target = Path(temp) / 'result.txt'
            with self.assertRaises(ValueError):
                controller.run(profile, request, target, estimate_micros=0)
            self.assertEqual(transport.called, 0)
            result = controller.run(profile, request, target, approved=True, estimate_micros=0)
            self.assertEqual(target.read_text(), 'Local generated text')
            self.assertEqual(result['billed_cost'], 'unknown')
            with self.assertRaises(FileExistsError):
                controller.run(profile, request, target, approved=True, estimate_micros=0)
            self.assertEqual(transport.called, 1)

    def test_output_is_checked_before_any_billable_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'private'
            root.mkdir()
            transport = LocalTransport()
            controller = ConnectionsController(root, transport=transport)
            profile = ProviderProfile('local', 'Local model', 'openai-compatible',
                'http://127.0.0.1:8080/v1', 'local-model', ('llm',), local=True)
            request = ProviderRequest('llm', 'Synthetic task')
            for output in (root / 'library.sqlite', Path(temp) / 'CON.txt', Path(temp) / 'audio:secret.mp3'):
                with self.subTest(output=output), self.assertRaises(ValueError):
                    controller.run(profile, request, output, approved=True, estimate_micros=0)
            with patch('tempfile.mkstemp', side_effect=OSError('full disk')):
                with self.assertRaises(OSError):
                    controller.run(profile, request, Path(temp) / 'result.txt', approved=True, estimate_micros=0)
            self.assertEqual(transport.called, 0)

    def test_post_generation_disk_failure_warns_against_rebilling(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'private'
            root.mkdir()
            transport = LocalTransport()
            controller = ConnectionsController(root, transport=transport)
            profile = ProviderProfile('local', 'Local', 'openai-compatible',
                'http://127.0.0.1:8080/v1', 'model', ('llm',), local=True)
            with patch('os.fsync', side_effect=OSError('full disk')):
                with self.assertRaisesRegex(ValueError, 'do not repeat the paid request'):
                    controller.run(profile, ProviderRequest('llm', 'Test'), Path(temp) / 'result.txt',
                                   approved=True, estimate_micros=0)
            self.assertEqual(transport.called, 1)



class MenuMnemonicTests(unittest.TestCase):
    def test_actual_settings_and_tools_keep_distinct_access_keys_and_dispatch(self):
        """Headless installer protocol; native wx remains a separate gate."""
        from types import SimpleNamespace
        from carr_library.ui import api_connections, assistant_dialog

        # Other top-level captions in the current DesktopFrame composition.
        # Language actions reuse Audio; document conversion reuses Tools.
        prefix = ('&File', '&Library', '&Research', '&Bibliography', '&Academic',
                  '&Help', '&Source', 'Rea&ding', 'A&udio')
        menus = [(caption, Mock()) for caption in prefix]
        bar = SimpleNamespace(
            Append=lambda menu, caption: menus.append((caption, menu)),
            FindMenu=lambda title: next((index for index, (caption, _) in enumerate(menus)
                                         if caption.replace('&', '') == title), -1),
            GetMenu=lambda index: menus[index][1],
        )
        bindings = []
        frame = SimpleNamespace(menu_bar=bar, GetMenuBar=lambda: bar,
            Bind=lambda event, handler, item: bindings.append((event, handler, item)))
        wx = SimpleNamespace(Menu=Mock(side_effect=Mock), ID_ANY=-1,
                             NOT_FOUND=-1, EVT_MENU=object())
        with patch.dict('sys.modules', {'wx': wx}), \
                patch.object(api_connections, 'show_api_connections') as show_connections, \
                patch.object(assistant_dialog, 'show_assistant') as show_assistant:
            api_connections.install_api_connection_actions(frame)
            assistant_dialog.install_assistant_actions(frame)
            bar.Append(Mock(), '&Options')
            captions = [caption for caption, _ in menus]
            access_keys = [caption.split('&', 1)[1][0].casefold()
                           for caption in captions if '&' in caption]
            self.assertEqual(len(access_keys), len(set(access_keys)))
            self.assertEqual([caption.replace('&', '') for caption in captions],
                ['File', 'Library', 'Research', 'Bibliography', 'Academic', 'Help',
                 'Source', 'Reading', 'Audio', 'Settings', 'Tools', 'Options'])
            settings = bar.GetMenu(bar.FindMenu('Settings'))
            tools = bar.GetMenu(bar.FindMenu('Tools'))
            settings.Append.assert_called_once_with(wx.ID_ANY, 'AI and API &connections...')
            tools.Append.assert_called_once_with(wx.ID_ANY, 'Local assistant...\tCtrl+Alt+J')
            self.assertEqual(len(bindings), 2)
            for (_, handler, item), menu in zip(bindings, (settings, tools)):
                self.assertIs(item, menu.Append.return_value)
                handler(None)
            self.assertTrue(all(event is wx.EVT_MENU for event, _, _ in bindings))
            show_connections.assert_called_once_with(frame)
            show_assistant.assert_called_once_with(frame)

if __name__ == '__main__':
    unittest.main()

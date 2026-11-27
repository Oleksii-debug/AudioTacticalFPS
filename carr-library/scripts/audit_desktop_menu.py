"""Inspect the real main-window menu tree from an explicit source checkout."""
from argparse import ArgumentParser
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import wx


def menu_items(menu, path, bound):
    for item in menu.GetMenuItems():
        if item.IsSeparator():
            continue
        label = item.GetItemLabel()
        caption, _, shortcut = label.partition("\t")
        location = [*path, caption]
        if item.IsSubMenu():
            yield from menu_items(item.GetSubMenu(), location, bound)
        else:
            yield {
                "menu_path": location, "shortcut": shortcut, "enabled_at_start": item.IsEnabled(),
                "bound_handler": bound.get(item.GetId()), "interaction": "NOT_RUN",
            }


def inspect_source(source):
    sys.path.insert(0, str(source / "src"))
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame

    class AuditedFrame(DesktopFrame):
        def __init__(self, library):
            self.audit_handlers = {}
            super().__init__(library)

        def Bind(self, event, handler, source=None, id=wx.ID_ANY, id2=wx.ID_ANY):
            if event is wx.EVT_MENU and handler is not None:
                command = source.GetId() if source is not None else id
                if command != wx.ID_ANY:
                    self.audit_handlers[command] = getattr(handler, "__qualname__", type(handler).__qualname__)
            return super().Bind(event, handler, source=source, id=id, id2=id2)

    with TemporaryDirectory(prefix="resonance-menu-audit-") as temporary:
        app = wx.App(False)
        frame = AuditedFrame(LibraryApp(Path(temporary).resolve() / "library"))
        try:
            bar = frame.GetMenuBar()
            menus = [bar.GetMenuLabel(index) for index in range(bar.GetMenuCount())]
            items = [
                row
                for index, label in enumerate(menus)
                for row in menu_items(bar.GetMenu(index), [label], frame.audit_handlers)
            ]
            return {
                "mode": "source/native-wx",
                "top_level_menus": menus, "items": items, "manual_nvda": "NOT_RUN",
                "runtime_menu_open_and_actions": "NOT_RUN",
            }
        finally:
            frame.cancel_event.set()
            frame._closing = True
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            app.Yield()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = inspect_source(args.source.resolve(strict=True))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    unbound = [item["menu_path"] for item in result["items"]
               if item["enabled_at_start"] and item["bound_handler"] is None]
    print(json.dumps({"menus": len(result["top_level_menus"]), "items": len(result["items"]),
                      "enabled_without_direct_binding": unbound}, ensure_ascii=False))
    return int(bool(unbound))


if __name__ == "__main__":
    raise SystemExit(main())

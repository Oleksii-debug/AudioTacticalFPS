"""Native explicit output-device selection; no playback or saved default."""
from dataclasses import dataclass

from ..audio.output_devices import OutputDevice, output_devices, resolve_output_device


@dataclass(frozen=True)
class OutputChoice:
    device: OutputDevice | None


def choose_output_device(parent, *, current=None, guard=lambda: None, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    guard()
    devices = output_devices()
    guard()
    entries = [None, *devices]
    labels = ['System default output (selected by Windows)', *[item.label for item in devices]]
    selected = 0
    if current is not None:
        selected = next((i for i, item in enumerate(entries)
                         if item is not None and item.identity == current.identity), -1)
        if selected < 0:
            entries.append(current)
            labels.append('Unavailable previous choice: ' + current.label + '; choose another output')
            selected = len(entries) - 1
    message = ('Choose where this reader or Studio preview will play. Stop playback before changing it. '
               'The choice lasts for this window and does not change the Windows default.')
    if not devices:
        message += '\nNo Windows output device is currently available.'

    class DeviceDialog(wx.SingleChoiceDialog):
        def Destroy(self):
            self.disposed = True
            return super().Destroy() if self else False

    dialog = DeviceDialog(parent, message, 'Playback output device', labels)
    dialog.disposed = False
    native_id = dialog.GetId()

    def destroyed(event):
        if event.GetId() == native_id:
            dialog.disposed = True
        event.Skip()

    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    dialog.SetName('Choose an explicit playback output device')
    dialog.SetSelection(selected)
    try:
        answer = dialog.ShowModal()
        guard()
        if dialog.disposed or not dialog or answer != wx.ID_OK:
            return None
        index = dialog.GetSelection()
        if type(index) is not int or not 0 <= index < len(entries):
            raise ValueError('Choose an output from the displayed device list.')
        chosen = entries[index]
        if chosen is not None:
            chosen = resolve_output_device(chosen)
        guard()
        if dialog.disposed or not dialog:
            return None
        return OutputChoice(chosen)
    finally:
        if not dialog.disposed and dialog:
            dialog.Destroy()

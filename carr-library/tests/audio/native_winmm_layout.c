/* Compare ctypes with the runner's real Windows SDK, not copied declarations. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <mmsystem.h>
#include <stddef.h>
#include <stdio.h>

#define BEGIN(name, type) \
    printf("\"%s\":{\"size\":%zu,\"alignment\":%zu,\"fields\":{", \
           name, sizeof(type), (size_t)__alignof(type))
#define FIELD(name, type, field) \
    printf("\"%s\":%zu", name, offsetof(type, field))
#define NEXT() putchar(',')
#define END() printf("}}")

int main(void) {
    putchar('{');
    BEGIN("_Open", MCI_WAVE_OPEN_PARMSW);
    FIELD("callback", MCI_WAVE_OPEN_PARMSW, dwCallback); NEXT();
    FIELD("device", MCI_WAVE_OPEN_PARMSW, wDeviceID); NEXT();
    FIELD("device_type", MCI_WAVE_OPEN_PARMSW, lpstrDeviceType); NEXT();
    FIELD("element", MCI_WAVE_OPEN_PARMSW, lpstrElementName); NEXT();
    FIELD("alias", MCI_WAVE_OPEN_PARMSW, lpstrAlias); NEXT();
    FIELD("buffer_seconds", MCI_WAVE_OPEN_PARMSW, dwBufferSeconds); END(); NEXT();
    BEGIN("_Generic", MCI_GENERIC_PARMS);
    FIELD("callback", MCI_GENERIC_PARMS, dwCallback); END(); NEXT();
    BEGIN("_Set", MCI_WAVE_SET_PARMS);
    FIELD("callback", MCI_WAVE_SET_PARMS, dwCallback); NEXT();
    FIELD("time_format", MCI_WAVE_SET_PARMS, dwTimeFormat); NEXT();
    FIELD("audio", MCI_WAVE_SET_PARMS, dwAudio); NEXT();
    FIELD("input", MCI_WAVE_SET_PARMS, wInput); NEXT();
    FIELD("output", MCI_WAVE_SET_PARMS, wOutput); NEXT();
    FIELD("format_tag", MCI_WAVE_SET_PARMS, wFormatTag); NEXT();
    FIELD("reserved2", MCI_WAVE_SET_PARMS, wReserved2); NEXT();
    FIELD("channels", MCI_WAVE_SET_PARMS, nChannels); NEXT();
    FIELD("reserved3", MCI_WAVE_SET_PARMS, wReserved3); NEXT();
    FIELD("sample_rate", MCI_WAVE_SET_PARMS, nSamplesPerSec); NEXT();
    FIELD("bytes_per_second", MCI_WAVE_SET_PARMS, nAvgBytesPerSec); NEXT();
    FIELD("block_align", MCI_WAVE_SET_PARMS, nBlockAlign); NEXT();
    FIELD("reserved4", MCI_WAVE_SET_PARMS, wReserved4); NEXT();
    FIELD("bits", MCI_WAVE_SET_PARMS, wBitsPerSample); NEXT();
    FIELD("reserved5", MCI_WAVE_SET_PARMS, wReserved5); END(); NEXT();
    BEGIN("_Position", MCI_SEEK_PARMS);
    FIELD("callback", MCI_SEEK_PARMS, dwCallback); NEXT();
    FIELD("position", MCI_SEEK_PARMS, dwTo); END(); NEXT();
    BEGIN("_Play", MCI_PLAY_PARMS);
    FIELD("callback", MCI_PLAY_PARMS, dwCallback); NEXT();
    FIELD("start", MCI_PLAY_PARMS, dwFrom); NEXT();
    FIELD("end", MCI_PLAY_PARMS, dwTo); END(); NEXT();
    BEGIN("_Status", MCI_STATUS_PARMS);
    FIELD("callback", MCI_STATUS_PARMS, dwCallback); NEXT();
    FIELD("result", MCI_STATUS_PARMS, dwReturn); NEXT();
    FIELD("item", MCI_STATUS_PARMS, dwItem); NEXT();
    FIELD("track", MCI_STATUS_PARMS, dwTrack); END();
    puts("}");
    return 0;
}

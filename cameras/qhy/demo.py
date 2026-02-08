import os
import re
import time
from ctypes import (
    CFUNCTYPE,
    POINTER,
    byref,
    c_char,
    c_char_p,
    c_double,
    c_int,
    c_uint8,
    c_uint16,
    c_uint32,
    c_ulong,
    c_void_p,
    create_string_buffer,
)
from datetime import datetime

import numpy as np
from astropy.io import fits
from PIL import Image as PIL_image
from qcam.image2ascii import np_array_to_ascii
from qcam.qCam import Qcam

cam = Qcam(
    os.path.join(
        os.path.dirname(__file__), "sdk", "2024-12-26-stable", "x64", "qhyccd.dll"
    )
)
assert cam.so is not None, "Failed to load QHY SDK"

cam.so.GetQHYCCDParamMinMaxStep.argtypes = [
    c_void_p,
    c_int,
    POINTER(c_double),
    POINTER(c_double),
    POINTER(c_double),
]
cam.so.GetQHYCCDParamMinMaxStep.restype = c_int

cam.so.GetQHYCCDControlName.argtypes = [c_void_p, c_int, c_char_p]
cam.so.GetQHYCCDControlName.restype = c_int

CONTROL_IDS = []


def get_controls():
    def ok(rc):  # QHY SDK tends to use 0 = success
        return rc == 0

    def minmaxstep(h, cid):
        lo = c_double()
        hi = c_double()
        step = c_double()
        fn = getattr(cam.so, "GetQHYCCDParamMinMaxStep")
        rc = fn(h, c_int(cid), byref(lo), byref(hi), byref(step))
        return ok(rc), lo.value, hi.value, step.value

    # 1) Get CONTROL_* IDs to test
    # If your wrapper already defines CONTROL_* constants, import those instead of parsing.

    assert cam.so is not None, "Failed to load QHY SDK"
    n = cam.so.ScanQHYCCD()
    print(f"found {n=} cameras")
    assert n > 0
    sn = create_string_buffer(64)
    assert cam.so.GetQHYCCDId(0, sn) == 0

    h = cam.so.OpenQHYCCD(sn)

    # (Option A) import constants from your own Python wrapper module:
    #   from qhydefs import CONTROL_EXPOSURE, CONTROL_GAIN, ...
    #   CONTROL_IDS = [CONTROL_EXPOSURE, CONTROL_GAIN, ...]
    #
    # (Option B) parse the installed header once to build the list dynamically:
    hdr_candidates = [
        os.path.join(
            os.path.dirname(__file__),
            "sdk",
            "include",
            "qhyccdstruct.h",
        )
    ]
    for hdr in hdr_candidates:
        if os.path.exists(hdr):
            for line in open(hdr, "r", errors="ignore"):
                # match: enum CONTROL_ID { CONTROL_EXPOSURE = 0, ... } or #define CONTROL_EXPOSURE ...
                # m = re.search(r'\bCONTROL_[A-Za-z0-9_]+\b', line)
                m = re.search(r"^\s*/\*\s*(\d+)\s*\*/\s*([A-Z][A-Za-z0-9_]*)\b", line)
                if m:
                    # name = m.group(0)
                    cid = m.group(1)
                    name = m.group(2)
                    # print(f"{cid=}, {name=}")
                    CONTROL_IDS.append((name, int(cid)))
            break

    print()
    print("class QHYControlId(enum.IntEnum):")
    for t in CONTROL_IDS:
        name = t[0]
        cid = t[1]
        print(f"    {name} = {cid}")

    print()
    print("class QHYControlRange(BaseModel):")
    print("    min: float")
    print("    max: float")
    print("    step: float")

    print()
    print("class QHYControl(BaseModel):")
    print("    id: QHYControlId")
    print("    name: str")
    print("    range: QHYControlRange | None = None")

    print()
    print("QHYControls: list[QHYControl] = [")
    for t in CONTROL_IDS:
        name = t[0]
        cid = t[1]
        rc = cam.so.IsQHYCCDControlAvailable(h, c_int(cid))
        if ok(rc):
            has_range, lo, hi, step = minmaxstep(h, cid)
            if has_range:
                print(
                    f"    QHYControl(name='{name}', id=QHYControlId.{name}, range=QHYControlRange(min={lo}, max={hi}, step={step})),"
                )
                # print(f"{cid:2} - {name:40} - range [{lo}, {hi}] step {step}")
            else:
                print(f"    QHYControl(name='{name}', id=QHYControlId.{name}),")
                # print(f"{cid:2} - {name}")
    print("]")


@CFUNCTYPE(None, c_char_p)
def pnp_in(cam_id):
    print("cam   + %s" % cam_id.decode("utf-8"))
    init_camera_param(cam_id)
    cam.camera_params[cam_id]["connect_to_pc"] = True
    os.makedirs(cam_id.decode("utf-8"), exist_ok=True)
    # select read mode
    assert cam.so is not None, "Failed to load QHY SDK"
    success = cam.so.GetReadModesNumber(
        cam_id, byref(cam.camera_params[cam_id]["read_mode_number"])
    )
    if success == cam.QHYCCD_SUCCESS:
        print("-  read mode - %s" % cam.camera_params[cam_id]["read_mode_number"].value)
        for read_mode_item_index in range(
            0, cam.camera_params[cam_id]["read_mode_number"].value
        ):
            read_mode_name = create_string_buffer(cam.STR_BUFFER_SIZE)
            cam.so.GetReadModeName(cam_id, read_mode_item_index, read_mode_name)
            print(
                "%s  %s %s"
                % (cam_id.decode("utf-8"), read_mode_item_index, read_mode_name.value)
            )
    else:
        print("GetReadModesNumber false")
        cam.camera_params[cam_id]["read_mode_number"] = c_uint32(0)

    # get_controls()
    read_mode_count = cam.camera_params[cam_id]["read_mode_number"].value
    if read_mode_count == 0:
        read_mode_count = 1
    for read_mode_index in range(0, read_mode_count):
        test_frame(cam_id, cam.stream_single_mode, cam.bit_depth_16, read_mode_index)
        test_frame(cam_id, cam.stream_live_mode, cam.bit_depth_16, read_mode_index)
        test_frame(cam_id, cam.stream_single_mode, cam.bit_depth_8, read_mode_index)
        test_frame(cam_id, cam.stream_live_mode, cam.bit_depth_8, read_mode_index)
        cam.so.CloseQHYCCD(cam.camera_params[cam_id]["handle"])


@CFUNCTYPE(None, c_char_p)
def pnp_out(cam_id):
    print("cam   - %s" % cam_id.decode("utf-8"))


def gui_start():
    assert cam.so is not None, "Failed to load QHY SDK"
    cam.so.RegisterPnpEventIn(pnp_in)
    cam.so.RegisterPnpEventOut(pnp_out)
    print("scan camera...")
    cam.so.InitQHYCCDResource()


def init_camera_param(cam_id):
    if not cam.camera_params.keys().__contains__(cam_id):
        cam.camera_params[cam_id] = {
            "connect_to_pc": False,
            "connect_to_sdk": False,
            "EXPOSURE": c_double(1000.0 * 1000.0),
            "GAIN": c_double(54.0),
            "CONTROL_BRIGHTNESS": c_int(0),
            "CONTROL_GAIN": c_int(6),
            "CONTROL_EXPOSURE": c_int(8),
            "CONTROL_CURTEMP": c_int(14),
            "CONTROL_CURPWM": c_int(15),
            "CONTROL_MANULPWM": c_int(16),
            "CONTROL_COOLER": c_int(18),
            "chip_width": c_double(),
            "chip_height": c_double(),
            "image_width": c_uint32(),
            "image_height": c_uint32(),
            "pixel_width": c_double(),
            "pixel_height": c_double(),
            "bits_per_pixel": c_uint32(),
            "mem_len": c_ulong(),
            "stream_mode": c_uint8(0),
            "channels": c_uint32(),
            "read_mode_number": c_uint32(),
            "read_mode_index": c_uint32(),
            "read_mode_name": c_char("-".encode("utf-8")),
            "prev_img_data": c_void_p(0),
            "prev_img": None,
            "handle": None,
        }


def test_frame(cam_id, stream_mode, bit_depth, read_mode):
    assert cam.so is not None, "Failed to load QHY SDK"
    print("open camera %s" % cam_id.decode("utf-8"))
    cam.camera_params[cam_id]["handle"] = cam.so.OpenQHYCCD(cam_id)
    if cam.camera_params[cam_id]["handle"] is None:
        print("open camera error %s" % cam_id)

    success = cam.so.SetQHYCCDReadMode(cam.camera_params[cam_id]["handle"], read_mode)
    cam.camera_params[cam_id]["stream_mode"] = c_uint8(stream_mode)
    success = cam.so.SetQHYCCDStreamMode(
        cam.camera_params[cam_id]["handle"], cam.camera_params[cam_id]["stream_mode"]
    )
    print("set StreamMode   =" + str(success))
    success = cam.so.InitQHYCCD(cam.camera_params[cam_id]["handle"])
    print("init Camera   =" + str(success))

    mode_name = create_string_buffer(cam.STR_BUFFER_SIZE)
    cam.so.GetReadModeName(cam_id, read_mode, mode_name)

    success = cam.so.SetQHYCCDBitsMode(
        cam.camera_params[cam_id]["handle"], c_uint32(bit_depth)
    )

    success = cam.so.GetQHYCCDChipInfo(
        cam.camera_params[cam_id]["handle"],
        byref(cam.camera_params[cam_id]["chip_width"]),
        byref(cam.camera_params[cam_id]["chip_height"]),
        byref(cam.camera_params[cam_id]["image_width"]),
        byref(cam.camera_params[cam_id]["image_height"]),
        byref(cam.camera_params[cam_id]["pixel_width"]),
        byref(cam.camera_params[cam_id]["pixel_height"]),
        byref(cam.camera_params[cam_id]["bits_per_pixel"]),
    )

    print("info.   =" + str(success))
    cam.camera_params[cam_id]["mem_len"] = cam.so.GetQHYCCDMemLength(
        cam.camera_params[cam_id]["handle"]
    )
    i_w = cam.camera_params[cam_id]["image_width"].value
    i_h = cam.camera_params[cam_id]["image_height"].value
    print("c-w:     " + str(cam.camera_params[cam_id]["chip_width"].value), end="")
    print("    c-h: " + str(cam.camera_params[cam_id]["chip_height"].value))
    print("p-w:     " + str(cam.camera_params[cam_id]["pixel_width"].value), end="")
    print("    p-h: " + str(cam.camera_params[cam_id]["pixel_height"].value))
    print("i-w:     " + str(i_w), end="")
    print("    i-h: " + str(i_h))
    print("bit: " + str(cam.camera_params[cam_id]["bits_per_pixel"].value))
    print("mem len: " + str(cam.camera_params[cam_id]["mem_len"]))

    val_temp = cam.so.GetQHYCCDParam(
        cam.camera_params[cam_id]["handle"], cam.CONTROL_CURTEMP
    )
    val_pwm = cam.so.GetQHYCCDParam(
        cam.camera_params[cam_id]["handle"], cam.CONTROL_CURPWM
    )

    # todo  c_uint8 c_uint16??
    if bit_depth == cam.bit_depth_16:
        print("using c_uint16()")
        cam.camera_params[cam_id]["prev_img_data"] = (
            c_uint16 * int(cam.camera_params[cam_id]["mem_len"] / 2)
        )()
    else:
        print("using c_uint8()")
        cam.camera_params[cam_id]["prev_img_data"] = (
            c_uint8 * cam.camera_params[cam_id]["mem_len"]
        )()

    success = cam.QHYCCD_ERROR

    image_width_byref = c_uint32()
    image_height_byref = c_uint32()
    bits_per_pixel_byref = c_uint32()
    # TODO resolution
    cam.so.SetQHYCCDResolution(
        cam.camera_params[cam_id]["handle"],
        c_uint32(0),
        c_uint32(0),
        c_uint32(i_w),
        c_uint32(i_h),
    )

    if stream_mode == cam.stream_live_mode:
        success = cam.so.BeginQHYCCDLive(cam.camera_params[cam_id]["handle"])
        print("exp  Live = " + str(success))

    frame_counter = 0
    time_string = "---"
    retry_counter = 0  # todo error control
    live_mode_skip_frame = 0

    while frame_counter < 2:
        time_string = datetime.now().strftime("%Y%m%d%H%M%S")
        if stream_mode == cam.stream_single_mode:
            success = cam.so.ExpQHYCCDSingleFrame(cam.camera_params[cam_id]["handle"])
            print("exp  single = " + str(success))
        success = cam.so.SetQHYCCDParam(
            cam.camera_params[cam_id]["handle"], cam.CONTROL_EXPOSURE, c_double(20000)
        )
        success = cam.so.SetQHYCCDParam(
            cam.camera_params[cam_id]["handle"], cam.CONTROL_GAIN, c_double(30)
        )
        success = cam.so.SetQHYCCDParam(
            cam.camera_params[cam_id]["handle"], cam.CONTROL_OFFSET, c_double(40)
        )
        # success = cam.so.SetQHYCCDParam(cam.camera_params[cam_id]['handle'], CONTROL_EXPOSURE, EXPOSURE)
        if stream_mode == cam.stream_live_mode:
            success = cam.so.GetQHYCCDLiveFrame(
                cam.camera_params[cam_id]["handle"],
                byref(image_width_byref),
                byref(image_height_byref),
                byref(bits_per_pixel_byref),
                byref(cam.camera_params[cam_id]["channels"]),
                byref(cam.camera_params[cam_id]["prev_img_data"]),
            )
            print("read  single = " + str(success))
        if stream_mode == cam.stream_single_mode:
            success = cam.so.GetQHYCCDSingleFrame(
                cam.camera_params[cam_id]["handle"],
                byref(image_width_byref),
                byref(image_height_byref),
                byref(bits_per_pixel_byref),
                byref(cam.camera_params[cam_id]["channels"]),
                byref(cam.camera_params[cam_id]["prev_img_data"]),
            )
            print("read  single = " + str(success))
        time.sleep(2)
        try_counter = 0
        while try_counter < 5 and success != cam.QHYCCD_SUCCESS:
            try_counter += 1
            print("success != 0  = " + str(success))
            time.sleep(1)

        if stream_mode == cam.stream_live_mode:
            live_mode_skip_frame += 1
            if live_mode_skip_frame < 3:
                print("skip frame in live mode  [%s]" % live_mode_skip_frame)
                continue
        frame_counter += 1

        cam.camera_params[cam_id]["prev_img"] = np.ctypeslib.as_array(
            cam.camera_params[cam_id]["prev_img_data"]
        )
        print("---------------->" + str(len(cam.camera_params[cam_id]["prev_img"])))
        image_size = i_h * i_w
        print("image size =     " + str(image_size))
        print(
            "prev_img_list sub length-->"
            + str(len(cam.camera_params[cam_id]["prev_img"]))
        )
        print("Image W=" + str(i_w) + "        H=" + str(i_h))
        cam.camera_params[cam_id]["prev_img"] = cam.camera_params[cam_id]["prev_img"][
            0:image_size
        ]
        image = np.reshape(cam.camera_params[cam_id]["prev_img"], (i_h, i_w))

        stream_mode_str = "stream_mode"
        read_mode_name_str = mode_name.value.decode("utf-8").replace(" ", "_")
        bit_depth_str = "bit_dep"
        if stream_mode == cam.stream_live_mode:
            stream_mode_str = "live"
        else:
            stream_mode_str = "single"
        if bit_depth == cam.bit_depth_16:
            bit_depth_str = "16bit"
        else:
            bit_depth_str = "8bit"

        if bit_depth == cam.bit_depth_8:
            pil_image = PIL_image.fromarray(image)
            # pil_image_save = PIL_image.fromarray(image).convert('L')
            pil_image.save(
                "%s/%s_%s.bmp" % (cam_id.decode("utf-8"), time_string, frame_counter)
            )
            pil_image = pil_image.resize((400, 400))
            # pil_image.show()
            ascii_img = np_array_to_ascii(pil_image, 50, 0.5, False)
            for row in ascii_img:
                print(row)

        hdu = fits.PrimaryHDU(image)
        hdul = fits.HDUList([hdu])
        hdul.writeto(
            "%s/%s_%s_str_%s_mode_%s_%s.fits"
            % (
                cam_id.decode("utf-8"),
                time_string,
                frame_counter,
                stream_mode_str,
                read_mode_name_str,
                bit_depth_str,
            )
        )

        print(
            "----   readMode %s / stream %s / bit %s / frame %s --------->"
            % (read_mode, stream_mode, bit_depth, frame_counter),
            end="",
        )
        time.sleep(1)


print("path: %s" % os.path.dirname(__file__))

gui_start()
print("=    type q to quit        =")
command = ""
while command != "q":
    command = input()

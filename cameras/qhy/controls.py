import ctypes

from pydantic import BaseModel


class QHYControlId:
    """QHY control IDs as per a QHY600U3 camera."""

    CONTROL_BRIGHTNESS = ctypes.c_int(0)
    CONTROL_CONTRAST = ctypes.c_int(1)
    CONTROL_WBR = ctypes.c_int(2)
    CONTROL_WBB = ctypes.c_int(3)
    CONTROL_WBG = ctypes.c_int(4)
    CONTROL_GAMMA = ctypes.c_int(5)
    CONTROL_GAIN = ctypes.c_int(6)
    CONTROL_OFFSET = ctypes.c_int(7)
    CONTROL_EXPOSURE = ctypes.c_int(8)
    CONTROL_SPEED = ctypes.c_int(9)
    CONTROL_TRANSFERBIT = ctypes.c_int(10)
    CONTROL_CHANNELS = ctypes.c_int(11)
    CONTROL_USBTRAFFIC = ctypes.c_int(12)
    CONTROL_ROWNOISERE = ctypes.c_int(13)
    CONTROL_CURTEMP = ctypes.c_int(14)
    CONTROL_CURPWM = ctypes.c_int(15)
    CONTROL_MANULPWM = ctypes.c_int(16)
    CONTROL_CFWPORT = ctypes.c_int(17)
    CONTROL_COOLER = ctypes.c_int(18)
    CONTROL_ST4PORT = ctypes.c_int(19)
    CAM_COLOR = ctypes.c_int(20)
    CAM_BIN1X1MODE = ctypes.c_int(21)
    CAM_BIN2X2MODE = ctypes.c_int(22)
    CAM_BIN3X3MODE = ctypes.c_int(23)
    CAM_BIN4X4MODE = ctypes.c_int(24)
    CAM_MECHANICALSHUTTER = ctypes.c_int(25)
    CAM_TRIGER_INTERFACE = ctypes.c_int(26)
    CAM_TECOVERPROTECT_INTERFACE = ctypes.c_int(27)
    CAM_SINGNALCLAMP_INTERFACE = ctypes.c_int(28)
    CAM_FINETONE_INTERFACE = ctypes.c_int(29)
    CAM_SHUTTERMOTORHEATING_INTERFACE = ctypes.c_int(30)
    CAM_CALIBRATEFPN_INTERFACE = ctypes.c_int(31)
    CAM_CHIPTEMPERATURESENSOR_INTERFACE = ctypes.c_int(32)
    CAM_USBREADOUTSLOWEST_INTERFACE = ctypes.c_int(33)
    CAM_8BITS = ctypes.c_int(34)
    CAM_16BITS = ctypes.c_int(35)
    CAM_GPS = ctypes.c_int(36)
    CAM_IGNOREOVERSCAN_INTERFACE = ctypes.c_int(37)
    QHYCCD_3A_AUTOEXPOSURE = ctypes.c_int(39)
    QHYCCD_3A_AUTOFOCUS = ctypes.c_int(40)
    CONTROL_AMPV = ctypes.c_int(41)
    CONTROL_VCAM = ctypes.c_int(42)
    CAM_VIEW_MODE = ctypes.c_int(43)
    CONTROL_CFWSLOTSNUM = ctypes.c_int(44)
    IS_EXPOSING_DONE = ctypes.c_int(45)
    ScreenStretchB = ctypes.c_int(46)
    ScreenStretchW = ctypes.c_int(47)
    CONTROL_DDR = ctypes.c_int(48)
    CAM_LIGHT_PERFORMANCE_MODE = ctypes.c_int(49)
    CAM_QHY5II_GUIDE_MODE = ctypes.c_int(50)
    DDR_BUFFER_CAPACITY = ctypes.c_int(51)
    DDR_BUFFER_READ_THRESHOLD = ctypes.c_int(52)
    DefaultGain = ctypes.c_int(53)
    DefaultOffset = ctypes.c_int(54)
    OutputDataActualBits = ctypes.c_int(55)
    OutputDataAlignment = ctypes.c_int(56)
    CAM_SINGLEFRAMEMODE = ctypes.c_int(57)
    CAM_LIVEVIDEOMODE = ctypes.c_int(58)
    CAM_IS_COLOR = ctypes.c_int(59)
    CONTROL_MAX_ID_Error = ctypes.c_int(61)
    CAM_HUMIDITY = ctypes.c_int(62)
    CAM_PRESSURE = ctypes.c_int(63)
    CONTROL_VACUUM_PUMP = ctypes.c_int(64)
    CONTROL_SensorChamberCycle_PUMP = ctypes.c_int(65)
    CAM_32BITS = ctypes.c_int(66)
    CAM_Sensor_ULVO_Status = ctypes.c_int(67)
    CAM_SensorPhaseReTrain = ctypes.c_int(68)
    CAM_InitConfigFromFlash = ctypes.c_int(69)
    CAM_TRIGER_MODE = ctypes.c_int(70)
    CAM_TRIGER_OUT = ctypes.c_int(71)
    CAM_BURST_MODE = ctypes.c_int(72)
    CAM_SPEAKER_LED_ALARM = ctypes.c_int(73)
    CAM_WATCH_DOG_FPGA = ctypes.c_int(74)
    CAM_BIN6X6MODE = ctypes.c_int(75)
    CAM_BIN8X8MODE = ctypes.c_int(76)
    CAM_GlobalSensorGPSLED = ctypes.c_int(77)
    CONTROL_ImgProc = ctypes.c_int(78)
    CONTROL_RemoveRBI = ctypes.c_int(79)
    CONTROL_GlobalReset = ctypes.c_int(80)
    CONTROL_FrameDetect = ctypes.c_int(81)
    CAM_GainDBConversion = ctypes.c_int(82)
    CAM_CurveSystemGain = ctypes.c_int(83)
    CAM_CurveFullWell = ctypes.c_int(84)
    CAM_CurveReadoutNoise = ctypes.c_int(85)
    CAM_UseAverageBinning = ctypes.c_int(86)
    CONTROL_OUTSIDE_PUMP_V2 = ctypes.c_int(87)
    CONTROL_AUTOEXPOSURE = ctypes.c_int(88)
    CONTROL_AUTOEXPTargetBrightness = ctypes.c_int(89)
    CONTROL_AUTOEXPSampleArea = ctypes.c_int(90)
    CONTROL_AUTOEXPexpMaxMS = ctypes.c_int(91)
    CONTROL_AUTOEXPgainMax = ctypes.c_int(92)
    CONTROL_Error_Led = ctypes.c_int(93)
    CONTROL_AUTOWHITEBALANCE = ctypes.c_int(1024)
    CONTROL_ImageStabilization = ctypes.c_int(1030)
    CONTROL_GAINdB = ctypes.c_int(1031)
    CONTROL_DPC = ctypes.c_int(1032)
    CONTROL_DPC_value = ctypes.c_int(1033)
    CONTROL_HDR = ctypes.c_int(1034)
    CONTROL_HDR_L_k = ctypes.c_int(1035)
    CONTROL_HDR_L_b = ctypes.c_int(1036)
    CONTROL_HDR_x = ctypes.c_int(1037)
    CONTROL_HDR_showKB = ctypes.c_int(1038)


class QHYControlRange(BaseModel):
    min: float
    max: float
    step: float


class QHYControl(BaseModel):
    id: ctypes.c_int
    name: str
    range: QHYControlRange | None = None
    model_config = {"arbitrary_types_allowed": True}


#
# A list of known QHY controls with their ranges where applicable.
# This can be used to validate settings and provide UI elements.
# Produced from a QHY600U3 camera using the demo.py script.
#
qhy_controls: list[QHYControl] = [
    QHYControl(
        name="CONTROL_BRIGHTNESS",
        id=QHYControlId.CONTROL_BRIGHTNESS,
        range=QHYControlRange(min=-1.0, max=1.0, step=0.1),
    ),
    QHYControl(
        name="CONTROL_CONTRAST",
        id=QHYControlId.CONTROL_CONTRAST,
        range=QHYControlRange(min=-1.0, max=1.0, step=0.1),
    ),
    QHYControl(
        name="CONTROL_GAMMA",
        id=QHYControlId.CONTROL_GAMMA,
        range=QHYControlRange(min=0.0, max=2.0, step=0.1),
    ),
    QHYControl(
        name="CONTROL_GAIN",
        id=QHYControlId.CONTROL_GAIN,
        range=QHYControlRange(min=0.0, max=200.0, step=1.0),
    ),
    QHYControl(
        name="CONTROL_OFFSET",
        id=QHYControlId.CONTROL_OFFSET,
        range=QHYControlRange(min=0.0, max=255.0, step=1.0),
    ),
    QHYControl(
        name="CONTROL_EXPOSURE",
        id=QHYControlId.CONTROL_EXPOSURE,
        range=QHYControlRange(min=1.0, max=3600000000.0, step=1.0),
    ),
    QHYControl(
        name="CONTROL_TRANSFERBIT",
        id=QHYControlId.CONTROL_TRANSFERBIT,
        range=QHYControlRange(min=8.0, max=16.0, step=8.0),
    ),
    QHYControl(
        name="CONTROL_USBTRAFFIC",
        id=QHYControlId.CONTROL_USBTRAFFIC,
        range=QHYControlRange(min=0.0, max=60.0, step=1.0),
    ),
    QHYControl(
        name="CONTROL_CURTEMP",
        id=QHYControlId.CONTROL_CURTEMP,
        range=QHYControlRange(min=-50.0, max=50.0, step=0.5),
    ),
    QHYControl(
        name="CONTROL_CURPWM",
        id=QHYControlId.CONTROL_CURPWM,
        range=QHYControlRange(min=0.0, max=255.0, step=1.0),
    ),
    QHYControl(
        name="CONTROL_MANULPWM",
        id=QHYControlId.CONTROL_MANULPWM,
        range=QHYControlRange(min=0.0, max=255.0, step=1.0),
    ),
    QHYControl(name="CONTROL_CFWPORT", id=QHYControlId.CONTROL_CFWPORT),
    QHYControl(
        name="CONTROL_COOLER",
        id=QHYControlId.CONTROL_COOLER,
        range=QHYControlRange(min=-50.0, max=50.0, step=0.5),
    ),
    QHYControl(name="CAM_BIN1X1MODE", id=QHYControlId.CAM_BIN1X1MODE),
    QHYControl(name="CAM_BIN2X2MODE", id=QHYControlId.CAM_BIN2X2MODE),
    QHYControl(name="CAM_BIN3X3MODE", id=QHYControlId.CAM_BIN3X3MODE),
    QHYControl(name="CAM_BIN4X4MODE", id=QHYControlId.CAM_BIN4X4MODE),
    QHYControl(name="CAM_TRIGER_INTERFACE", id=QHYControlId.CAM_TRIGER_INTERFACE),
    QHYControl(name="CAM_8BITS", id=QHYControlId.CAM_8BITS),
    QHYControl(name="CAM_16BITS", id=QHYControlId.CAM_16BITS),
    QHYControl(name="CAM_GPS", id=QHYControlId.CAM_GPS),
    QHYControl(name="CONTROL_VCAM", id=QHYControlId.CONTROL_VCAM),
    QHYControl(name="CONTROL_CFWSLOTSNUM", id=QHYControlId.CONTROL_CFWSLOTSNUM),
    QHYControl(name="CAM_SINGLEFRAMEMODE", id=QHYControlId.CAM_SINGLEFRAMEMODE),
    QHYControl(name="CAM_LIVEVIDEOMODE", id=QHYControlId.CAM_LIVEVIDEOMODE),
    QHYControl(name="CAM_HUMIDITY", id=QHYControlId.CAM_HUMIDITY),
    QHYControl(name="CAM_PRESSURE", id=QHYControlId.CAM_PRESSURE),
    QHYControl(name="CAM_32BITS", id=QHYControlId.CAM_32BITS),
    QHYControl(name="CAM_Sensor_ULVO_Status", id=QHYControlId.CAM_Sensor_ULVO_Status),
    QHYControl(name="CAM_InitConfigFromFlash", id=QHYControlId.CAM_InitConfigFromFlash),
    QHYControl(
        name="CAM_TRIGER_MODE",
        id=QHYControlId.CAM_TRIGER_MODE,
        range=QHYControlRange(min=0.0, max=1.0, step=1.0),
    ),
    QHYControl(name="CAM_TRIGER_OUT", id=QHYControlId.CAM_TRIGER_OUT),
    QHYControl(name="CAM_BURST_MODE", id=QHYControlId.CAM_BURST_MODE),
    QHYControl(name="CONTROL_ImgProc", id=QHYControlId.CONTROL_ImgProc),
    QHYControl(name="CONTROL_RemoveRBI", id=QHYControlId.CONTROL_RemoveRBI),
    QHYControl(name="CAM_GainDBConversion", id=QHYControlId.CAM_GainDBConversion),
    QHYControl(
        name="CONTROL_AUTOEXPOSURE",
        id=QHYControlId.CONTROL_AUTOEXPOSURE,
        range=QHYControlRange(min=0.0, max=3.0, step=1.0),
    ),
    QHYControl(
        name="CONTROL_AUTOEXPTargetBrightness",
        id=QHYControlId.CONTROL_AUTOEXPTargetBrightness,
        range=QHYControlRange(min=15.0, max=240.0, step=1.0),
    ),
    QHYControl(
        name="CONTROL_AUTOEXPSampleArea",
        id=QHYControlId.CONTROL_AUTOEXPSampleArea,
        range=QHYControlRange(min=0.0, max=3.0, step=1.0),
    ),
    QHYControl(
        name="CONTROL_AUTOEXPexpMaxMS",
        id=QHYControlId.CONTROL_AUTOEXPexpMaxMS,
        range=QHYControlRange(min=1.0, max=5000.0, step=1.0),
    ),
    QHYControl(
        name="CONTROL_AUTOEXPgainMax",
        id=QHYControlId.CONTROL_AUTOEXPgainMax,
        range=QHYControlRange(min=0.0, max=200.0, step=1.0),
    ),
]

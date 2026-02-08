import ctypes

# Type aliases
qhyccd_handle_p = ctypes.c_void_p

# Prototypes for all STDCALL functions


def set_ctypes_prototypes(sdk):
    sdk.OutputQHYCCDDebug.argtypes = [ctypes.c_char_p]
    sdk.OutputQHYCCDDebug.restype = None

    sdk.SetQHYCCDAutoDetectCamera.argtypes = [ctypes.c_bool]
    sdk.SetQHYCCDAutoDetectCamera.restype = None

    # sdk.SetQHYCCDLogPath.argtypes = [ctypes.c_char_p]
    # sdk.SetQHYCCDLogPath.restype = None

    # sdk.SetQHYCCDLogLevel.argtypes = [ctypes.c_uint8]
    # sdk.SetQHYCCDLogLevel.restype = None

    # sdk.EnableQHYCCDMessage.argtypes = [ctypes.c_bool]
    # sdk.EnableQHYCCDMessage.restype = None

    # sdk.set_histogram_equalization.argtypes = [ctypes.c_bool]
    # sdk.set_histogram_equalization.restype = None

    # sdk.EnableQHYCCDLogFile.argtypes = [ctypes.c_bool]
    # sdk.EnableQHYCCDLogFile.restype = None

    # sdk.SetQHYCCDArrayCamSync.argtypes = [qhyccd_handle_p, ctypes.c_bool]
    # sdk.SetQHYCCDArrayCamSync.restype = ctypes.c_uint32

    sdk.SetQHYCCDSingleFrameTimeOut.argtypes = [qhyccd_handle_p, ctypes.c_uint32]
    sdk.SetQHYCCDSingleFrameTimeOut.restype = ctypes.c_uint32

    sdk.GetTimeStamp.argtypes = []
    sdk.GetTimeStamp.restype = ctypes.c_char_p

    # sdk.CheckQHYCCDDeviceDriverIO.argtypes = [ctypes.c_uint32]
    # sdk.CheckQHYCCDDeviceDriverIO.restype = ctypes.c_uint32

    sdk.InitQHYCCDResource.argtypes = []
    sdk.InitQHYCCDResource.restype = ctypes.c_uint32

    sdk.ReleaseQHYCCDResource.argtypes = []
    sdk.ReleaseQHYCCDResource.restype = ctypes.c_uint32

    sdk.ScanQHYCCD.argtypes = []
    sdk.ScanQHYCCD.restype = ctypes.c_uint32

    sdk.GetQHYCCDId.argtypes = [ctypes.c_uint32, ctypes.c_char_p]
    sdk.GetQHYCCDId.restype = ctypes.c_uint32

    sdk.GetQHYCCDModel.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    sdk.GetQHYCCDModel.restype = ctypes.c_uint32

    sdk.OpenQHYCCD.argtypes = [ctypes.c_char_p]
    sdk.OpenQHYCCD.restype = qhyccd_handle_p

    sdk.CloseQHYCCD.argtypes = [qhyccd_handle_p]
    sdk.CloseQHYCCD.restype = ctypes.c_uint32

    sdk.SetQHYCCDStreamMode.argtypes = [qhyccd_handle_p, ctypes.c_uint8]
    sdk.SetQHYCCDStreamMode.restype = ctypes.c_uint32

    sdk.InitQHYCCD.argtypes = [qhyccd_handle_p]
    sdk.InitQHYCCD.restype = ctypes.c_uint32

    sdk.IsQHYCCDControlAvailable.argtypes = [qhyccd_handle_p, ctypes.c_int]
    sdk.IsQHYCCDControlAvailable.restype = ctypes.c_uint32

    sdk.GetQHYCCDControlName.argtypes = [qhyccd_handle_p, ctypes.c_int, ctypes.c_char_p]
    sdk.GetQHYCCDControlName.restype = ctypes.c_uint32

    sdk.SetQHYCCDParam.argtypes = [qhyccd_handle_p, ctypes.c_int, ctypes.c_double]
    sdk.SetQHYCCDParam.restype = ctypes.c_uint32

    sdk.GetQHYCCDParam.argtypes = [qhyccd_handle_p, ctypes.c_int]
    sdk.GetQHYCCDParam.restype = ctypes.c_double

    sdk.GetQHYCCDParamMinMaxStep.argtypes = [
        qhyccd_handle_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
    ]
    sdk.GetQHYCCDParamMinMaxStep.restype = ctypes.c_uint32

    sdk.SetQHYCCDResolution.argtypes = [
        qhyccd_handle_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    sdk.SetQHYCCDResolution.restype = ctypes.c_uint32

    sdk.GetQHYCCDMemLength.argtypes = [qhyccd_handle_p]
    sdk.GetQHYCCDMemLength.restype = ctypes.c_uint32

    sdk.ExpQHYCCDSingleFrame.argtypes = [qhyccd_handle_p]
    sdk.ExpQHYCCDSingleFrame.restype = ctypes.c_uint32

    sdk.GetQHYCCDSingleFrame.argtypes = [
        qhyccd_handle_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint8),
    ]
    sdk.GetQHYCCDSingleFrame.restype = ctypes.c_uint32

    sdk.CancelQHYCCDExposing.argtypes = [qhyccd_handle_p]
    sdk.CancelQHYCCDExposing.restype = ctypes.c_uint32

    sdk.CancelQHYCCDExposingAndReadout.argtypes = [qhyccd_handle_p]
    sdk.CancelQHYCCDExposingAndReadout.restype = ctypes.c_uint32

    sdk.BeginQHYCCDLive.argtypes = [qhyccd_handle_p]
    sdk.BeginQHYCCDLive.restype = ctypes.c_uint32

    sdk.GetQHYCCDLiveFrame.argtypes = [
        qhyccd_handle_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint8),
    ]
    sdk.GetQHYCCDLiveFrame.restype = ctypes.c_uint32

    sdk.StopQHYCCDLive.argtypes = [qhyccd_handle_p]
    sdk.StopQHYCCDLive.restype = ctypes.c_uint32

    sdk.SetQHYCCDBinMode.argtypes = [qhyccd_handle_p, ctypes.c_uint32, ctypes.c_uint32]
    sdk.SetQHYCCDBinMode.restype = ctypes.c_uint32

    sdk.SetQHYCCDBitsMode.argtypes = [qhyccd_handle_p, ctypes.c_uint32]
    sdk.SetQHYCCDBitsMode.restype = ctypes.c_uint32

    sdk.ControlQHYCCDTemp.argtypes = [qhyccd_handle_p, ctypes.c_double]
    sdk.ControlQHYCCDTemp.restype = ctypes.c_uint32

    sdk.ControlQHYCCDGuide.argtypes = [
        qhyccd_handle_p,
        ctypes.c_uint32,
        ctypes.c_uint16,
    ]
    sdk.ControlQHYCCDGuide.restype = ctypes.c_uint32

    sdk.SendOrder2QHYCCDCFW.argtypes = [
        qhyccd_handle_p,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ]
    sdk.SendOrder2QHYCCDCFW.restype = ctypes.c_uint32

    sdk.GetQHYCCDCFWStatus.argtypes = [qhyccd_handle_p, ctypes.c_char_p]
    sdk.GetQHYCCDCFWStatus.restype = ctypes.c_uint32

    sdk.IsQHYCCDCFWPlugged.argtypes = [qhyccd_handle_p]
    sdk.IsQHYCCDCFWPlugged.restype = ctypes.c_uint32

    sdk.GetQHYCCDTrigerInterfaceNumber.argtypes = [
        qhyccd_handle_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    sdk.GetQHYCCDTrigerInterfaceNumber.restype = ctypes.c_uint32

    sdk.GetQHYCCDTrigerInterfaceName.argtypes = [
        qhyccd_handle_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
    ]
    sdk.GetQHYCCDTrigerInterfaceName.restype = ctypes.c_uint32

    sdk.SetQHYCCDTrigerInterface.argtypes = [qhyccd_handle_p, ctypes.c_uint32]
    sdk.SetQHYCCDTrigerInterface.restype = ctypes.c_uint32

    sdk.SetQHYCCDTrigerFunction.argtypes = [qhyccd_handle_p, ctypes.c_bool]
    sdk.SetQHYCCDTrigerFunction.restype = ctypes.c_uint32

    sdk.SetQHYCCDTrigerMode.argtypes = [qhyccd_handle_p, ctypes.c_uint32]
    sdk.SetQHYCCDTrigerMode.restype = ctypes.c_uint32

    sdk.EnableQHYCCDTrigerOut.argtypes = [qhyccd_handle_p]
    sdk.EnableQHYCCDTrigerOut.restype = ctypes.c_uint32

    sdk.EnableQHYCCDTrigerOutA.argtypes = [qhyccd_handle_p]
    sdk.EnableQHYCCDTrigerOutA.restype = ctypes.c_uint32

    sdk.SendSoftTriger2QHYCCDCam.argtypes = [qhyccd_handle_p]
    sdk.SendSoftTriger2QHYCCDCam.restype = ctypes.c_uint32

    sdk.SetQHYCCDTrigerFilterOnOff.argtypes = [qhyccd_handle_p, ctypes.c_bool]
    sdk.SetQHYCCDTrigerFilterOnOff.restype = ctypes.c_uint32

    sdk.SetQHYCCDTrigerFilterTime.argtypes = [qhyccd_handle_p, ctypes.c_uint32]
    sdk.SetQHYCCDTrigerFilterTime.restype = ctypes.c_uint32

    sdk.Bits16ToBits8.argtypes = [
        qhyccd_handle_p,
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint16,
        ctypes.c_uint16,
    ]
    sdk.Bits16ToBits8.restype = None

    sdk.HistInfo192x130.argtypes = [
        qhyccd_handle_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.POINTER(ctypes.c_uint8),
    ]
    sdk.HistInfo192x130.restype = None

    sdk.OSXInitQHYCCDFirmware.argtypes = [ctypes.c_char_p]
    sdk.OSXInitQHYCCDFirmware.restype = ctypes.c_uint32

    sdk.OSXInitQHYCCDFirmwareArray.argtypes = []
    sdk.OSXInitQHYCCDFirmwareArray.restype = ctypes.c_uint32

    # sdk.OSXInitQHYCCDAndroidFirmwareArray.argtypes = [
    #     ctypes.c_int,
    #     ctypes.c_int,
    #     ctypes.c_int,
    # ]
    # sdk.OSXInitQHYCCDAndroidFirmwareArray.restype = ctypes.c_uint32

    sdk.GetQHYCCDChipInfo.argtypes = [
        qhyccd_handle_p,
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    sdk.GetQHYCCDChipInfo.restype = ctypes.c_uint32

    sdk.GetQHYCCDEffectiveArea.argtypes = [
        qhyccd_handle_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    sdk.GetQHYCCDEffectiveArea.restype = ctypes.c_uint32

    sdk.GetQHYCCDOverScanArea.argtypes = [
        qhyccd_handle_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    sdk.GetQHYCCDOverScanArea.restype = ctypes.c_uint32

    sdk.GetQHYCCDCurrentROI.argtypes = [
        qhyccd_handle_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    sdk.GetQHYCCDCurrentROI.restype = ctypes.c_uint32

    sdk.GetQHYCCDImageStabilizationGravity.argtypes = [
        qhyccd_handle_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
    ]
    sdk.GetQHYCCDImageStabilizationGravity.restype = ctypes.c_uint32

    sdk.SetQHYCCDFocusSetting.argtypes = [
        qhyccd_handle_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    sdk.SetQHYCCDFocusSetting.restype = ctypes.c_uint32

    sdk.GetQHYCCDExposureRemaining.argtypes = [qhyccd_handle_p]
    sdk.GetQHYCCDExposureRemaining.restype = ctypes.c_uint32

    sdk.GetQHYCCDFWVersion.argtypes = [qhyccd_handle_p, ctypes.POINTER(ctypes.c_uint8)]
    sdk.GetQHYCCDFWVersion.restype = ctypes.c_uint32

    sdk.GetQHYCCDFPGAVersion.argtypes = [
        qhyccd_handle_p,
        ctypes.c_uint8,
        ctypes.POINTER(ctypes.c_uint8),
    ]
    sdk.GetQHYCCDFPGAVersion.restype = ctypes.c_uint32

    sdk.SetQHYCCDReadMode.argtypes = [qhyccd_handle_p, ctypes.c_uint32]
    sdk.SetQHYCCDReadMode.restype = ctypes.c_uint32

    sdk.GetReadModesNumber.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32)]
    sdk.GetReadModesNumber.restype = ctypes.c_uint32
    sdk.GetQHYCCDReadModeName.argtypes = [
        qhyccd_handle_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
    ]
    sdk.GetQHYCCDReadModeName.restype = ctypes.c_uint32

    # ...continue for all other STDCALL functions as needed...

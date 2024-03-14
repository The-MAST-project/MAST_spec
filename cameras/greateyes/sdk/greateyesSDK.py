# -*- coding: utf-8 -*-
"""
Created on Wed Mar 18 11:21:55 2020
20200512 Updated SetFunctions
20220214 Updated GetSensorOutputModeStrings (SDK 22.5)

@author: Marcel Behrendt

This python wrapper makes all functions from the greateyes.dll available to python users using either windows or linux operating systems.
The appropriate greateyes SDK needs to be located in a directory, included in PATH.

"""

import ctypes
import numpy as np
import sys
import platform
import time

if platform.system() == 'Windows':
    greateyesDLL = ctypes.WinDLL("C:\\Program Files\\greateyes\\greateyesVision\\greateyes.dll")
    c_PixelDataType16bit = ctypes.c_ushort
    c_PixelDataType32bit = ctypes.c_ulong
elif platform.system() == 'Linux':
    greateyesDLL = ctypes.CDLL("/usr/local/lib/libgreateyes.so")
    c_PixelDataType16bit = ctypes.c_ushort
    c_PixelDataType32bit = ctypes.c_uint


# 1. Constant
# 1.1 Possible value of statusMSG
# -------------------------------------------------------------------------------------------------------
StatusMSG = ''
c_Status = ctypes.c_int(16)
Status = int(16)

StatusMSG_list = ['']*17
StatusMSG_list[0] = 'camera detected and ok'
StatusMSG_list[1] = 'no camera detected'
StatusMSG_list[2] = 'there is a problem with the USB interface'
StatusMSG_list[3] = 'transferring data to cam failed - TimeOut!'
StatusMSG_list[4] = 'receiving data  from cam failed - TimeOut!'
StatusMSG_list[5] = 'no extern trigger signal within time window of TriggerTimeOut'
StatusMSG_list[6] = 'new cam detected - you need to perform CamSettings'
StatusMSG_list[7] = 'this DLL was not written for connected cam - please request new greateyes.dll'
StatusMSG_list[8] = 'one ore more parameters are out of range'
StatusMSG_list[9] = 'no new image data'
StatusMSG_list[10] = 'camera busy'
StatusMSG_list[11] = 'cooling turned off'
StatusMSG_list[12] = 'measurement stopped'
StatusMSG_list[13] = 'too many pixels for BurstMode. Set lower number of measurements or higher binning level'
StatusMSG_list[14] = 'timing table for selected readout speed not found'
StatusMSG_list[15] = 'function stoped but there is no critical error (no valid result; catched division by zero). please try to call function again.'
StatusMSG_list[16] = ''

# this function updates the status index and status string in python,
# while referring to the last returned index from the DLL
def UpdateStatus():
    global Status, c_Status, StatusMSG, StatusMSG_list
    
    Status = c_Status.value
    if Status in range(len(StatusMSG_list)):
        StatusMSG = StatusMSG_list[Status]
    else:
        StatusMSG = 'Status unknown'
        

# 1.2 Function Constant
# -------------------------------------------------------------------------------------------------------
TemperatureHardwareOption = int(42223)
maxPixelBurstTransfer = int(8823794)

sensorFeature_capacityMode = int(0)
sensorFeature_binningX = int(2)
sensorFeature_cropX = int(1)

readoutSpeed_50_kHz = int(50)
readoutSpeed_100_kHz = int(100)
readoutSpeed_250_kHz = int(250)
readoutSpeed_500_kHz = int(500)
readoutSpeed_1_MHz = int(1000)
readoutSpeed_3_MHz = int(3000)

connectionType_USB = int(0)
connectionType_Ethernet = int(3)

# -------------------------------------------------------------------------------------------------------

# 2. Exported DLL Functions
# -------------------------------------------------------------------------------------------------------

# 2.1 Setup camera interface (USB/Ethernet)

# -------------------------------------------------------------------------------------------------------

# sets the connection type to either USB or Ethernet
# IN: connectionType    connectionType_USB (0) (default): Camera connected via USB
#                       connectionType_Ethernet (3): Camera connected via Ethernet (TCP/IP)
# IN: ipAddress         IP address of greateyesCameraServer
# In: addr              index of connected devices; begins at addr = 0 for first device; max. 4 devices
# Out: statusMSG        updates index and string of status message
# Result: Bool          success true/false
def SetupCameraInterface(connectionType = connectionType_USB, ipAddress = '192.168.1.234', addr=0):
    # referring to DLL function
    geFunc = greateyesDLL.SetupCameraInterface
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_connectionType = ctypes.c_int(connectionType)
    ge_ipAddress = ctypes.c_char_p(ipAddress.encode('ASCII'))
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_connectionType,ge_ipAddress,ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# 2.2 Connecting to a greateyes camera server

# -------------------------------------------------------------------------------------------------------

# Necessary for connection via Ethernet only. Call this function to connect to a greateyesCameraServer which is connected to up to four cameras. (MultiCamMode)
# Suitable for operating multiple greateyes cameras with USB interface connected to a greateyes camera server.
# Result: Bool             success true/false
def ConnectToMultiCameraServer():
    # referring to DLL function
    geFunc = greateyesDLL.ConnectToMultiCameraServer
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = []

    # calling function
    retValue = geFunc()

    # returning return value
    return retValue

# -------------------------------------------------------------------------------------------------------

# Necessary for connection via Ethernet only. Call this function to connect to up to four greateyesCameraServers. Each greateyesCameraServer operates one camera. (MultiServerMode)
# Suitable for all greateyes cameras with ethernet interface.
# In: addr					index of connected devices; begins at addr = 0 for first device; max. 4 devices
# Result: Bool             success true/false
def ConnectToSingleCameraServer(addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.ConnectToSingleCameraServer
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int]

    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_addr)

    UpdateStatus()

    # returning return value
    return retValue

# -------------------------------------------------------------------------------------------------------

# In:      addr             index of connected devices; begins at addr = 0 for first device; max. 4 devices
#                           The function ignores the parameter addr when connected with ConnectToCameraServer() (MultiCamMode)
# Result:  Bool             success true/false
def DisconnectCameraServer(addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.DisconnectCameraServer
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int]

    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_addr)

    # returning return value
    return retValue

# -------------------------------------------------------------------------------------------------------

# 2.3 Connecting to a greateyes camera (USB/Ethernet)

# -------------------------------------------------------------------------------------------------------

# Result:               number of devices connected
#                       Call this funktion before calling ConnectCamera()
#                       Not required if connected with ConnectToSingleCameraServer() to a SingleCameraServer.
def GetNumberOfConnectedCams():
    # referring to DLL function
    geFunc = greateyesDLL.GetNumberOfConnectedCams
    geFunc.restype = ctypes.c_int

    # casting arguments
    geFunc.argtypes = []

    # calling function
    retValue = geFunc()

    # returning return value
    return retValue

# -------------------------------------------------------------------------------------------------------

#    -replaces CheckCamera function; connects up to 4 devices; Ethernet and USB
#    -call GetNumberOfConnectedCams before in case of connecting to cameras with USB interface directly or through a MultiCamMode
#
# Out:    model             list with 2 items
#                               1) modelID: internal model specific ID  (no serial number)
#                               2) modelStr: string of camera model
# Out:    statusMSG         updates index and string of status message
# In:     addr              index of connected devices; begins at addr = 0 for first device; max. 4 devices
# Result: Bool              success true/false
def ConnectCamera(model = [], addr=0):
    # referring to DLL function
    geFunc = greateyesDLL.ConnectCamera
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_modelId = ctypes.pointer(ctypes.c_int())
    ge_modelStr = ctypes.pointer(ctypes.c_char_p())
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_modelId,ge_modelStr,ge_statusMSG,ge_addr)

    # extracting values
    model.clear()
    model.append(ge_modelId.contents.value)
    model.append(ge_modelStr.contents.value.decode('ASCII'))

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# disconnects a single camera with the given addr index
# In:      addr             index of connected devices; begins at addr = 0 for first device
# Out:     statusMSG        updates index and string of status message
# Result:  Bool             success true/false
def DisconnectCamera(addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.DisconnectCamera
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# 2.4 Initialization of greateyes camera (USB/Ethernet)

# -------------------------------------------------------------------------------------------------------

# It is recommended to call InitCamera(..) at least one time after connecting to the camera.
# OUT:    statusMSG         updates index and string of status message
# In:     addr              index of connected devices; begins at addr = 0 for first device
# Result: Bool              success true/false
def InitCamera(addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.InitCamera
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    time.sleep(2)
    return retValue

# -------------------------------------------------------------------------------------------------------

# 2.5 Set Functions

# -------------------------------------------------------------------------------------------------------

# sets the exposure time for measurements
# IN:       exposureTime        exposure time [0..2^31] ms
# OUT:      statusMSG           updates index and string of status message
# In:       addr                index of connected devices; begins at addr = 0 for first device
# Result:   Bool                success true/false
def SetExposure(exposureTime, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.SetExposure
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_SetParameter = ctypes.c_int(exposureTime)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter, ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# sets the pixel frequency for measurements
# IN:       readoutSpeed        sets pixel clock to [0..6]
#                                   0 -> 1 MHz
#                                   3 -> 3 MHz
#                                   5 -> 500 kHz
#                                   6 -> 50 kHZ
# OUT:      statusMSG           updates index and string of status message
# In:       addr                index of connected devices; begins at addr = 0 for first device
# Result:   Bool                success true/false
def SetReadOutSpeed(readOutSpeed, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.SetReadOutSpeed
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_SetParameter = ctypes.c_int(readOutSpeed)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter, ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# sets binning options for measurements
# IN:       binningX        Number of pixels [1...numPixelInX] to be binned in x-direction (if supported by CCD)
#                           Note:   For backwards compatibility, when using cameras with
#                                   earlier firmware revisions (rev. 11 or lower) the binningX
#                                   parameter was realized in software thus it does not reduce the overall SNR.
#                                   Also the parameter is interpreted differently as shown below:
#                                       pow(2, binningX) = Number of pixels to be binned in x-direction
# IN:       binningY        Number of pixels [1...numPixelInX] to be binned in y-direction
#                           Note:   For backwards compatibility, when using cameras with
#                                   earlier firmware revisions (rev. 11 or lower) the binningY
#                                   parameter is interpreted differently as shown below:
#                                       0		No binning of lines
#                                       1		Binning of 2 lines
#                                       2		Binning of 4 lines
#                                       3		Binning of 8 lines
#                                       4		Binning of 16 lines
#                                       5		Binning of 32 lines
#                                       6 		Binning of 64 lines
#                                       7		Binning of 128 lines
#                                       8 		Full vertical binning
# OUT:      statusMSG       updates index and string of status message
# In:       addr            index of connected devices; begins at addr = 0 for first device
# Result:   Bool            success true/false


def SetBinningMode(binningX, binningY, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.SetBinningMode
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_SetParameter_X = ctypes.c_int(binningX)
    ge_SetParameter_Y = ctypes.c_int(binningY)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter_X, ge_SetParameter_Y, ge_statusMSG, ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# sets the exposure time for measurements
# IN:       openTime            time to wait before exposure [ms]
# IN:       closeTime           time to wait after exposure [ms]
# OUT:      statusMSG           updates index and string of status message
# In:       addr                index of connected devices; begins at addr = 0 for first device
# Result:   Bool                success true/false


def SetShutterTimings(openTime, closeTime, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.SetShutterTimings
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_SetParameter_o = ctypes.c_int(openTime)
    ge_SetParameter_c = ctypes.c_int(closeTime)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter_o, ge_SetParameter_c, ge_statusMSG, ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# sets the TTL shutter output of the camera high or low on function call.
# or sets the output to be handled automatically by the camera during measurements
# IN:       state               shutter mode
#                                   state = 0 --> close shutter		 (TTL Low)
#                                   state = 1 --> open shutter		 (TTL High)
#                                   state = 2 --> automatic shutter  (TTL High while image acquisition)
#                                   For automatic shutter it is necessary to set shutter open and close time with SetShutterTimings() function.
#                                   !!Automatic shutter does not work in combination with Burst - Mode.
# OUT:      statusMSG           updates index and string of status message
# In:       addr                index of connected devices; begins at addr = 0 for first device
# Result:   Bool                success true/false
def OpenShutter(state, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.OpenShutter
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_SetParameter = ctypes.c_int(state)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter, ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# Call this function to manually set the “SYNC” TTL trigger output high/low.
# IN:       syncHigh            sync output high/low
# OUT:      statusMSG           updates index and string of status message
# In:       addr                index of connected devices; begins at addr = 0 for first device
# Result:   Bool                success true/false


def SyncOutput(syncHigh, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.OpenShutter
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_SetParameter = ctypes.c_bool(syncHigh)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter, ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# IN:       numberOfMeasurements    number of measurements in series. Mind the maximum Number of Pixels, see above
# OUT:      statusMSG               updates index and string of status message
# In:       addr                    index of connected devices; begins at addr = 0 for first device
# Result:   Bool                    success true/false


def SetupBurstMode(numberOfMeasurements, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.SetupBurstMode
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_SetParameter = ctypes.c_int(numberOfMeasurements)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter, ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# IN:       status                  sets burst mode on/off
# OUT:      statusMSG               updates index and string of status message
# In:       addr                    index of connected devices; begins at addr = 0 for first device
# Result:   Bool                    success true/false


def ActivateBurstMode(status, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.ActivateBurstMode
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_SetParameter = ctypes.c_bool(status)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter, ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# IN:       col                     number of columns to read out
# IN:       line                    number of lines to read out
# OUT:      statusMSG               updates index and string of status message
# In:       addr                    index of connected devices; begins at addr = 0 for first device
# Result:   Bool                    success true/false


def SetupCropMode2D(col, line, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.SetupCropMode2D
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_SetParameter_col = ctypes.c_int(col)
    ge_SetParameter_line = ctypes.c_int(line)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter_col, ge_SetParameter_line, ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# IN:       status                  bool: sets crop mode on/off
# OUT:      statusMSG               updates index and string of status message
# In:       addr                    index of connected devices; begins at addr = 0 for first device
# Result:   Bool                    success true/false


def ActivateCropMode(status, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.ActivateCropMode
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_SetParameter = ctypes.c_bool(status)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter, ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# IN:       gainSetting             0 -> Low ( Max. Dyn. Range )
#                                   1 -> Std ( High Sensitivity )
# OUT:      statusMSG               updates index and string of status message
# In:       addr                    index of connected devices; begins at addr = 0 for first device
# Result:   Bool                    success true/false
def SetupGain(gainSetting, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.SetupGain
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_SetParameter = ctypes.c_int(gainSetting)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter, ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# IN:       capacityMode            false -> Standard ( Low Noise )
#                                   true -> Extended ( High Signal )
# OUT:      statusMSG               updates index and string of status message
# In:       addr                    index of connected devices; begins at addr = 0 for first device
# Result:   Bool                    success true/false
def SetupCapacityMode(capacityMode, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.SetupCapacityMode
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_SetParameter = ctypes.c_bool(capacityMode)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter, ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    time.sleep(2)
    return retValue

# -------------------------------------------------------------------------------------------------------

# IN:       safeFifoMode            true/false
#                                   default: true
# IN:       saveUsbMode             true/false
#                                   default: false
# Result:   Bool                    success true/false
def SetupTransferOptions(safeFifoMode = True, saveUsbMode = False):
    # referring to DLL function
    geFunc = greateyesDLL.SetupTransferOptions
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_bool, ctypes.c_bool]

    ge_SetParameter_Fifo = ctypes.c_bool(safeFifoMode)
    ge_SetParameter_USB = ctypes.c_bool(saveUsbMode)

    # calling function
    retValue = geFunc(ge_SetParameter_Fifo, ge_SetParameter_USB)

    # returning return value
    return retValue

# -------------------------------------------------------------------------------------------------------

# IN:       sensorOutputMode        [ 0 .. (NumberOfSensorOutputModes - 1) ]
# In:       addr                    index of connected devices; begins at addr = 0 for first device
# Result:   Bool                    success true/false
def SetupSensorOutputMode(sensorOutputMode, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.SetupSensorOutputMode
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.c_int]

    ge_SetParameter = ctypes.c_int(sensorOutputMode)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter, ge_addr)

    # returning return value
    return retValue

# -------------------------------------------------------------------------------------------------------

# OUT:      statusMSG               updates index and string of status message
# In:       addr                    index of connected devices; begins at addr = 0 for first device
# Result:   Integer                 number of cleared blocks
def ClearFifo(addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.ClearFifo
    geFunc.restype = ctypes.c_int

    # casting arguments
    geFunc.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# IN:       bytesPerPixel           [2 .. 4]
#                                   set bytes per pixel for cameras with 18 bit adc (max. 20 bit dynamic range through oversampling)
#                                   for cameras with 16 bit adc bytesPerPixel is always 2
# OUT:      statusMSG               updates index and string of status message
# In:       addr                    index of connected devices; begins at addr = 0 for first device
# Result:   Bool                    success true/false
def SetBitDepth(bytesPerPixel, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.SetBitDepth
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_SetParameter = ctypes.c_int(bytesPerPixel)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter, ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# IN:       extTriggerTimeOut       [0..65535]ms
#                                   set timeout for Startmeaseurement_V2() function with external trigger.
# In:       addr                    index of connected devices; begins at addr = 0 for first device
# Result:   Bool                    success true/false
def SetExtTriggerTimeOut(extTriggerTimeOut, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.SetExtTriggerTimeOut
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.c_int]

    ge_SetParameter = ctypes.c_int(extTriggerTimeOut)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter,ge_addr)

    # returning return value
    return retValue
	
# -------------------------------------------------------------------------------------------------------
	
# In: timeOut				>= 0 ms (Default : 3000ms)
# Result: bool	            success true/false
#							Set timeout for function calls. If the timeout is set to 0, functions will return busy immediately. 
#							Otherwise the function will try to get a slot for the time of setted timeout. 	
def SetBusyTimeout(timeout):
	# referring to DLL function
    geFunc = greateyesDLL.SetBusyTimeout
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int]

    ge_SetParameter = ctypes.c_int(timeout)

    # calling function
    retValue = geFunc(ge_SetParameter)

    # returning return value
    return retValue
				
	
# -------------------------------------------------------------------------------------------------------

 # Switch backside LED's on/off
# In: status				true/false 
# Result: bool	            success true/false
def SetLEDStatus(status, addr=0):
	# referring to DLL function
    geFunc = greateyesDLL.SetLEDStatus
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_SetParameter = ctypes.c_bool(status)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_SetParameter, ge_statusMSG,ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# 2.6 Get Functions
# -------------------------------------------------------------------------------------------------------

# returns the DLL Version as string
def GetDLLVersion():
    # referring to DLL function
    geFunc = greateyesDLL.GetDLLVersion
    geFunc.restype = ctypes.c_char_p

    # casting arguments
    geFunc.argtypes = [ctypes.POINTER(ctypes.c_int)]

    size = ctypes.pointer(ctypes.c_int())

    # calling function
    DLLVersion = geFunc(size)

    # returning return value
    return DLLVersion.decode('ASCII')
# -------------------------------------------------------------------------------------------------------

# returns the firmware version of the camera as integer value
# In: addr					index of connected devices; begins at addr = 0 for first device
# Result: Integer			firmware version
def GetFirmwareVersion(addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.GetFirmwareVersion
    geFunc.restype = ctypes.c_int

    # casting arguments
    geFunc.argtypes = [ctypes.c_int]

    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_addr)

    # returning return value
    return retValue

# -------------------------------------------------------------------------------------------------------

# information about the image array to be expected.
# returns list with 3 values:
#   1) width of the image
#   2) height of the image
#   3) bits per pixel, i.e. depth of signal, given in byte
#       bytesPerPixel = 2  -> 16 Bit -> Use a 16 bit type pointer or an 8 bit type pointer and allocate 2 bytes per pixel.
#       bytesPerPixel = 3  -> 24 Bit -> Use an 8 bit type pointer and allocate 3 bytes per pixel. Save 25% memory compared to 32 bit output.
#       bytesPerPixel = 4  -> 32 Bit -> Use a 32 bit type pointer or an 8 bit type pointer and allocate 4 bytes per pixel. Easy to handle but 8 bit are useless.
#
# In: addr			index of connected devices; begins at addr = 0 for first device
def GetImageSize(addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.GetImageSize
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_width = ctypes.pointer(ctypes.c_int())
    ge_height = ctypes.pointer(ctypes.c_int())
    ge_bytesPerPixel = ctypes.pointer(ctypes.c_int())
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_width, ge_height, ge_bytesPerPixel, ge_addr)

    # extracting values
    if (retValue == True):
        results = [ge_width.contents.value, ge_height.contents.value, ge_bytesPerPixel.contents.value]
    else:
        results = [0,0,0]

    # returning return value
    return results

# -------------------------------------------------------------------------------------------------------

# returns size of each pixel
# In: addr      index of connected devices; begins at addr = 0 for first device
# Result:       physical length of a single pixel, given in micrometers. Assuming a square shaped pixel
def GetSizeOfPixel(addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.GetSizeOfPixel
    geFunc.restype = ctypes.c_int

    # casting arguments
    geFunc.argtypes = [ctypes.c_int]

    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_addr)

    # returning return value
    return retValue

# -------------------------------------------------------------------------------------------------------

# returns current state of DLL operation
# In: addr		index of connected devices; begins at addr = 0 for first device
# Result:       Boolean status of the DLL being busy or not
def DllIsBusy(addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.DllIsBusy
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int]

    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_addr)

    # returning return value
    return retValue

# -------------------------------------------------------------------------------------------------------

# returns maximum exposure time to be set
# In: addr		index of connected devices; begins at addr = 0 for first device
# Result:       max ExposureTime in ms supported by the camera model / firmware version
def GetMaxExposureTime(addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.GetMaxExposureTime
    geFunc.restype = ctypes.c_int

    # casting arguments
    geFunc.argtypes = [ctypes.c_int]

    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_addr)

    # returning return value
    return retValue

# -------------------------------------------------------------------------------------------------------

# returns maximum setting for horizontal binning to be set
# In:   addr	           index of connected devices; begins at addr = 0 for first device
# Out:  statusMSG      updates index and string of status message
# Result:              max. possible value for parameter binningX. (depends on sensor type and crop mode setting)
def GetMaxBinningX(addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.GetMaxBinningX
    geFunc.restype = ctypes.c_int

    # casting arguments
    geFunc.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_statusMSG, ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# returns maximum setting for vertical binning to be set
# In:   addr	           index of connected devices; begins at addr = 0 for first device
# Out:  statusMSG      updates index and string of status message
# Result:              max. possible value for parameter binningY. (depends on sensor type and crop mode setting)
def GetMaxBinningY(addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.GetMaxBinningY
    geFunc.restype = ctypes.c_int

    # casting arguments
    geFunc.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_statusMSG, ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# provides information about the supported sensor features of the camera.
# In: feature       possible features:
#                       0) sensorFeature_capacityMode -> Only sensors with this feature can operate in the capacity mode.
#                       1) sensorFeature_binningX -> Only sensors with this feature can bin in x - direction (serial).
#                       2) sensorFeature_cropX	-> Only sensors with this feature can operate in the crop mode.
# In: addr              index of connected devices; begins at addr = 0 for first device
# OUT: statusMSG        updates index and string of status message
# Result: Bool          sensor supports feature (true/false)
def SupportedSensorFeature(feature, addr=0):
    # referring to DLL function
    geFunc = greateyesDLL.SupportedSensorFeature
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_feature = ctypes.c_int(feature)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_feature, ge_statusMSG, ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# returns the number of output modes the specific camera model provides
# In: addr          index of connected devices; begins at addr = 0 for first device
#                   you get this ID from the function "ConnectCamera"
# Result: int       number of possible output modes for camera model.
#                   Usually a sensor has a single output only.
#                   For larger format sensors, e.g. 4096px x 4096px sensors,
#                   (modelID = 12) up to 10 output modes are specified.
def GetNumberOfSensorOutputModes(addr=0):
    # referring to DLL function
    geFunc = greateyesDLL.GetNumberOfSensorOutputModes
    geFunc.restype = ctypes.c_int

    # casting arguments
    geFunc.argtypes = [ctypes.c_int]

    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_addr)

    # returning return value
    return retValue

# -------------------------------------------------------------------------------------------------------

# returns a string containing information on the single indexed output mode
# In: index         [ 0 .. (NumberOfSensorOutputModes - 1) ]
# In: addr	         index of connected devices; begins at addr = 0 for first device
# Result: string    output mode string
def GetSensorOutputModeStrings(index, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.GetSensorOutputModeStrings
    geFunc.restype = ctypes.c_char_p

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.c_int]

    om_index = ctypes.c_int(index)
    ge_addr = ctypes.c_int(addr)

    # calling function
    ModeString = geFunc(om_index, ge_addr)

    # extracting values
    OutputModeString = ModeString.decode('ASCII')

    # returning return value
    return OutputModeString

# -------------------------------------------------------------------------------------------------------

# returns the time that passed between the last call for measurement and the data being available in the DLL
# In: addr	         index of connected devices; begins at addr = 0 for first device
# Result: FLoat      time needed (exposure time + read out) in ms
def GetLastMeasTimeNeeded(addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.GetLastMeasTimeNeeded
    geFunc.restype = ctypes.c_float

    # casting arguments
    geFunc.argtypes = [ctypes.c_int]

    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_addr)

    # returning return value
    return retValue

# -------------------------------------------------------------------------------------------------------

# 2.7 Temperature Control Functions

# -------------------------------------------------------------------------------------------------------

# This function inits the sensor cooling control. It is based upon the coolingHardware Parameter.
#   To get the CoolingHardware for your camera please have a
#   look in the SDK folder of your software release. There you
#   can find a “TemperatureHardwareOption.txt”. If you can
#   not find, please contact us.
# In:       addr                index of connected devices; begins at addr = 0 for first device
# OUT:      statusMSG           updates index and string of status message
# Result:   Int                 Temp_limits:        list with 2 items:
#                                   minTemperature      min. possible value in °C for parameter temperature of function
#                                   TemperatureControl_SetTemperature()
#                                   maxTemperature      max. possible value in °C for parameter temperature of function
#                                   TemperatureControl_SetTemperature()
def TemperatureControl_Init(coolingHardware = TemperatureHardwareOption, addr=0):
    # referring to DLL function
    geFunc = greateyesDLL.TemperatureControl_Init
    geFunc.restype = ctypes.c_int

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_coolingHardware = ctypes.c_int(coolingHardware)
    ge_minTemperature = ctypes.pointer(ctypes.c_int())
    ge_maxTemperature = ctypes.pointer(ctypes.c_int())
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    NumberOfCoolingLevels = geFunc(ge_coolingHardware, ge_minTemperature, ge_maxTemperature, ge_statusMSG, ge_addr)

    # extracting values
    if NumberOfCoolingLevels >= 1:
        Temp_limits = [ge_minTemperature.contents.value, ge_maxTemperature.contents.value]
    else:
        Temp_limits = [-300,-300]

    # returning return value
    UpdateStatus()
    return Temp_limits

# -------------------------------------------------------------------------------------------------------

# returns the actual temperature of the indexed thermistor
# In:       thermistor      0: sensor temperature   1: backside temperature
#                               It is recommended to check the backside temperature frequently. Maximum backside temperature is about 55°C.
#                               If the backside temperature gets higher than 55°C, please turn off the cooling control and contact greateyes GmbH.
# In:       addr            index of connected devices; begins at addr = 0 for first device
# OUT:      statusMSG       updates index and string of status message
# Result:   Int             temperature in °C --> ( Kelvin - 273.15 )
def TemperatureControl_GetTemperature(thermistor = 0, addr=0):
    # referring to DLL function
    geFunc = greateyesDLL.TemperatureControl_GetTemperature
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_thermistor = ctypes.c_int(thermistor)
    ge_temperature = ctypes.pointer(ctypes.c_int())
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    worked = geFunc(ge_thermistor, ge_temperature, ge_statusMSG, ge_addr)

    # extracting values
    if worked:
        retValue = ge_temperature.contents.value
    else:
        retValue = -300

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# adjust temperature of CCD sensor in °C --> (Kelvin - 273.15).
# In:       temperature      value must be between TemperatureControl_GetMinTemperature() and TemperatureControl_GetMaxTemperature()
# In:       addr            index of connected devices; begins at addr = 0 for first device
# OUT:      statusMSG       updates index and string of status message
# Result:   Bool            success true/false
def TemperatureControl_SetTemperature(temperature, addr=0):
    # referring to DLL function
    geFunc = greateyesDLL.TemperatureControl_SetTemperature
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int,ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_temperature = ctypes.c_int(temperature)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_temperature, ge_statusMSG, ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# switch sensor cooling off completely
# In:       addr            index of connected devices; begins at addr = 0 for first device
# OUT:      statusMSG       updates index and string of status message
# Result:   Bool            success true/false
def TemperatureControl_SwitchOff(addr=0):
    # referring to DLL function
    geFunc = greateyesDLL.TemperatureControl_SwitchOff
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_statusMSG, ge_addr)

    # returning return value
    UpdateStatus()
    return retValue

# -------------------------------------------------------------------------------------------------------

# 2.8 Image Acquisition
# -------------------------------------------------------------------------------------------------------

# Starts measurement in a thread
# IN:       correctBias         if true, each line will be intensity corrected dependent on the dark pixel values left and right of this line
# IN:       showSync            if true, the sync output of the camera will rise during exposure time otherwise it remains low.
# IN:       showShutter         use auto shutter
# IN:       triggerMode         external trigger
# Out:      statusMSG           index of status message
# In:       addr                index of connected devices; begins at addr = 0 for first device
# Result:   Bool                success true/false
def StartMeasurement_DynBitDepth(correctBias = False, showSync = True, showShutter = False, triggerMode = False, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.StartMeasurement_DynBitDepth
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_bool, ctypes.c_bool, ctypes.c_bool, ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    ge_correctBias = ctypes.c_bool(correctBias)
    ge_showSync = ctypes.c_bool(showSync)
    ge_showShutter = ctypes.c_bool(showShutter)
    ge_triggerMode = ctypes.c_bool(triggerMode)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_correctBias, ge_showSync, ge_showShutter, ge_triggerMode, ge_statusMSG, ge_addr)

    # returning return value
    UpdateStatus
    return retValue

# -------------------------------------------------------------------------------------------------------

# Gets measurement started with StartMeasurement() function. Use DllIsBusy() function to check whether measurement is ready. Use GetImageSize() function to get size of sample.
# IN:       pInDataStart        pointer to image array
# Out:      statusMSG           index of status message
# In:       addr                index of connected devices; begins at addr = 0 for first device
# Result:   Bool                success true/false
def GetMeasurementData_DynBitDepth(addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.GetMeasurementData_DynBitDepth
    geFunc.restype = ctypes.c_bool

    # allocating memory
    DataDimensions = GetImageSize(addr)
    if (DataDimensions[2] == 2):
        c_PixelDataType = c_PixelDataType16bit
        py_PixelDataType = np.uint16
    elif ((DataDimensions[2] == 3) or (DataDimensions[2] == 4)):
        c_PixelDataType = c_PixelDataType32bit
        py_PixelDataType = np.uint32
    else:
        print('GetImageSize returned unexpected value for bitDepth.')
        print('DataDimensions:', DataDimensions)
        sys.exit()

    # casting arguments
    geFunc.argtypes = [ctypes.POINTER(c_PixelDataType), ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    array_class = c_PixelDataType*DataDimensions[0]*DataDimensions[1]
    array_inst = array_class()
    Mem = ctypes.pointer(array_inst)
    ge_pInDataStart = ctypes.cast(Mem, ctypes.POINTER(c_PixelDataType))

    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    worked = geFunc(ge_pInDataStart, ge_statusMSG, ge_addr)

    if worked:
        imageData=np.ctypeslib.as_array(array_inst)
    else:
        imageData = np.ndarray((DataDimensions[0],DataDimensions[1]),dtype=py_PixelDataType)
    # returning return value
    UpdateStatus
    return imageData

# -------------------------------------------------------------------------------------------------------

# Start and wait for measurement. Function blocks for duration of measurement. Function returns measurement in pInDataStart mem. Use GetImageSize() function to get size of sample.
# (This function has more that six input parameters and might not work with some environments (for example Matlab). Alternatively use StartMeasurement_DynBitDepth() and GetMeasurementData_DynBitDepth()  )
# IN:       correctBias         if true, each line will be intensity corrected dependent on the dark pixel values left and right of this line
# IN:       showSync            if true, the sync output of the camera will rise during exposure time otherwise it remains low.
# IN:       showShutter         use auto shutter
# IN:       triggerMode         external trigger
# IN:       triggerTimeOut      timeout for external trigger
# Out:      statusMSG           index of status message
# In:       addr                index of connected devices; begins at addr = 0 for first device
# Result:   Bool                success true/false
def PerformMeasurement_Blocking_DynBitDepth(correctBias = False, showSync = True, showShutter = False, triggerMode = False, triggerTimeOut=30, addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.PerformMeasurement_Blocking_DynBitDepth
    geFunc.restype = ctypes.c_bool

    # allocating memory
    DataDimensions = GetImageSize(addr)
    if (DataDimensions[2] == 2):
        c_PixelDataType = c_PixelDataType16bit
        py_PixelDataType = np.uint16
    elif ((DataDimensions[2] == 3) or (DataDimensions[2] == 4)):
        c_PixelDataType = c_PixelDataType32bit
        py_PixelDataType = np.uint32
    else:
        print('GetImageSize returned unexpected value for bitDepth.')
        print('DataDimensions:', DataDimensions)
        sys.exit()

    # casting arguments
    geFunc.argtypes = [ctypes.c_bool, ctypes.c_bool, ctypes.c_bool, ctypes.c_bool, ctypes.c_int,ctypes.POINTER(c_PixelDataType) , ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    array_class = c_PixelDataType*DataDimensions[0]*DataDimensions[1]
    array_inst = array_class()
    Mem = ctypes.pointer(array_inst)
    ge_pInDataStart = ctypes.cast(Mem, ctypes.POINTER(c_PixelDataType))

    ge_correctBias = ctypes.c_bool(correctBias)
    ge_showSync = ctypes.c_bool(showSync)
    ge_showShutter = ctypes.c_bool(showShutter)
    ge_triggerMode = ctypes.c_bool(triggerMode)
    ge_triggerTimeOut = ctypes.c_int(triggerTimeOut)
    global c_Status
    ge_statusMSG = ctypes.pointer(c_Status)
    ge_addr = ctypes.c_int(addr)

    # calling function
    worked = geFunc(ge_correctBias, ge_showSync, ge_showShutter, ge_triggerMode, ge_triggerTimeOut, ge_pInDataStart, ge_statusMSG, ge_addr)

    if worked:
        imageData=np.ctypeslib.as_array(array_inst)
    else:
        imageData = np.ndarray((DataDimensions[0],DataDimensions[1]),dtype=py_PixelDataType)

    # returning return value
    UpdateStatus
    return imageData

# -------------------------------------------------------------------------------------------------------

# Stops measurement started by StartMeasurement() function. Do not work with PerformMeasurement_Blocking().
# After stopped measurement the DllIsBusy() function will return false and the GetMeasurementData() funktion will return StatusMSG=12 (Message_MeasurementStopped).
# In:       addr	       index of connected devices; begins at addr = 0 for first device
# Result:              success true/false
def StopMeasurement(addr = 0):
    # referring to DLL function
    geFunc = greateyesDLL.StopMeasurement
    geFunc.restype = ctypes.c_bool

    # casting arguments
    geFunc.argtypes = [ctypes.c_int]

    ge_addr = ctypes.c_int(addr)

    # calling function
    retValue = geFunc(ge_addr)

    # returning return value
    return retValue

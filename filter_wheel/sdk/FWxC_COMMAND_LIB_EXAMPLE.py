try:
    from FWxC_COMMAND_LIB import *
    import time
except OSError as ex:
    print("Warning:",ex)


def Func(serialNumber):
   hdl = FWxCOpen(serialNumber,115200,3)
   #or check by "FWxCIsOpen(devs[0])"
   if(hdl < 0):
       print("Connect ",serialNumber, "fail" )
       return -1;
   else:
       print("Connect ",serialNumber, "successful")
   
   result = FWxCIsOpen(serialNumber)
   if(result<0):
       print("Open failed ")
   else:
       print("FWxC Is Open ")

   id = []
   result = FWxCGetId(hdl, id)
   if(result<0):
       print("Get Id fail ",result)
   else:
       print("Get Id :",id)
   
   result = FWxCSetTriggerMode(hdl, 0) #0:input mode,1:output mode
   if(result<0):
       print("Set Trigger Mode fail" , result)
   else:
       print("Trigger Mode is" , "input mode")

   triggermode=[0]
   triggermodeList={0:"input mode", 1:"output mode"}
   result=FWxCGetTriggerMode(hdl,triggermode)
   if(result<0):
      print("Get Trigger Mode fail",result)
   else:
      print("Get Trigger Mode :",triggermodeList.get(triggermode[0]))

   result = FWxCSetSpeedMode(hdl, 0) #0:slow speed,1:high speed
   if(result<0):
       print("Set Speed Mode fail" , result)
   else:
       print("Speed Mode is" , "slow speed")

   speedmode=[0]
   speedmodeList={0:"slow speed", 1:"high speed"}
   result=FWxCGetSpeedMode(hdl,speedmode)
   if(result<0):
      print("Get Speed Mode fail",result)
   else:
      print("Get Speed Mode :",speedmodeList.get(speedmode[0]))

   result = FWxCSetSensorMode(hdl, 0) #0:Sensors turn off,1: Sensors remain active
   if(result<0):
       print("Set Sensor Mode fail" , result)
   else:
       print("Sensor Mode is" , "Sensors turn off")

   sensormode=[0]
   sensormodeList={0:"Sensors turn off", 1:"Sensors remain active"}
   result=FWxCGetSensorMode(hdl,sensormode)
   if(result<0):
      print("Get Sensor Mode fail",result)
   else:
      print("Get Sensor Mode :",sensormodeList.get(sensormode[0]))

   result = FWxCSetPosition(hdl, 3) 
   if(result<0):
       print("Set Position Mode fail" , result)
   else:
       print("Position  is" , 3)
   time.sleep(10)

   position=[0]   
   result=FWxCGetPosition(hdl,position)
   if(result<0):
      print("Get Position fail",result)
   else:
      print("Get Position :",position)

   #result = FWxCSetPositionCount(hdl, 12) 
   #if(result<0):
   #    print("Set Position Count fail" , result)
   #else:
   #    print("Position Count  is" , 12)

   positioncount=[0]   
   result=FWxCGetPositionCount(hdl,positioncount)
   if(result<0):
      print("Get Position Count fail",result)
   else:
      print("Get Position Count :",positioncount)

   result=FWxCSave(hdl)
   if(result<0):
       print("Save fail" , result)
   else:
       print("Save successfully!")
       
   return hdl

def main():
    print("*** FWxC device python example ***")
    try:
        devs = FWxCListDevices()
        print(devs)
        if(len(devs)<=0):
           print('There is no devices connected')
           exit()

        FWxC= devs[0]

        hdl=Func(FWxC[0])
        print("---------------------------Device Func run finished-------------------------")
        
        FWxCClose(hdl)
        
    except Exception as ex:
        print("Warning:", ex)
    print("*** End ***")
    input()
main()


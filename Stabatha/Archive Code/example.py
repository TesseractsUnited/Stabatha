from Phidget22.Phidget import *
from Phidget22.Devices.VoltageRatioInput import *
import time

#Declare any event handlers here. These will be called every time the associated event occurs.

def onVoltageRatioChange(self, voltageRatio):
	print("VoltageRatio: " + str(voltageRatio))

def onAttach(self):
	print("Attach!")

def onDetach(self):
	print("Detach!")

def main():
	#Declare any necessary variables here

	#Create your Phidget channels
	voltageRatioInput0 = VoltageRatioInput()

	#Set addressing parameters to specify which channel to open (if any)
	voltageRatioInput0.setDeviceSerialNumber(797606)

	#Assign any event handlers you need before calling open so that no events are missed.
	voltageRatioInput0.setOnVoltageRatioChangeHandler(onVoltageRatioChange)
	voltageRatioInput0.setOnAttachHandler(onAttach)
	voltageRatioInput0.setOnDetachHandler(onDetach)
	

	#Open your Phidgets and wait for attachment
	voltageRatioInput0.openWaitForAttachment(5000)

	
	#Interact with your Phidgets here or in your event handlers.
	voltageRatioInput0.setDataInterval(8)

	try:
		input("Press Enter to Stop\n")
	except (Exception, KeyboardInterrupt):
		pass

	#Close your Phidgets once the program is done.
	voltageRatioInput0.close()

main()

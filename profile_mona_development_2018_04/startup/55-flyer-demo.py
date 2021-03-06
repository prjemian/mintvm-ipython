print(__file__)

"""
Demo of Flyer to simulate MONA tomo fly scan

:see: http://nsls-ii.github.io/ophyd/architecture.html#fly-able-interface
"""


import asyncio
from collections import deque, OrderedDict


class BusyRecord(Device):
    state = Component(EpicsSignal, "")
    output_link = Component(EpicsSignal, ".OUT")
    forward_link = Component(EpicsSignal, ".FLNK")
    

class SpinFlyer(Device):
    """
    one spin of the tomo stage, run as ophyd Flyer object
    
    Kickoff
    
    * motor starts at initial position
    * moved to pre-start (taxi) position
    * detector prepared for acquisition on motor increments
    * motion started towards end position (fly) in separate thread
    
    Complete
    
    * motor monitored for not moving status
    
    Collect
    
    * success = motor has reached end position
    * detector data saved
    """
    
    def __init__(self, 
                 motor, 
                 detector, 
                 busy, 
                 pre_start=-0.5, 
                 pos_start=-20, 
                 pos_finish=20, 
                 loop=None, 
                 **kwargs):
        super().__init__('', parent=None, **kwargs)
        self.motor = motor
        self.detector = detector
        self.busy = busy
        self.pre_start = pre_start
        self.return_position = motor.position
        self.pos_start = pos_start
        self.pos_finish = pos_finish
        self.stream_name = "spin_flyer_stream"
        
        self.poll_delay_s = 0.05
        self.num_spins = 1

        self._completion_status = None
        self._data = deque()
        self.loop = loop or asyncio.get_event_loop()
    
    def taxi(self):
        # pre_start position is far enough before pos_start to ramp up to speed
        position = self.pos_start + self.pre_start
        self.motor.move(position)
    
    def fly(self):
        self.motor.move(self.pos_finish)

    def set(self, value):       # interface for BlueSky plans
        """value is either Taxi or Fly"""
        if str(value).lower() not in ("fly", "taxi", "return"):
            msg = "value should be either Taxi, Fly, or Return."
            msg + " received " + str(value)
            raise ValueError(msg)

        if self.busy.value:
            raise RuntimeError("spin is operating")

        status = DeviceStatus(self)
        
        def action():
            """the real action of ``set()`` is here"""
            if str(value).lower() == "taxi":
                self.taxi()
            elif str(value).lower() == "fly":
                self.pre_fly()
                self.fly()
                self.post_fly()
            elif str(value).lower() == "return":
                self.motor.move(self.return_position)

        def run_and_wait():
            """handle the ``action()`` in a thread"""
            self.busy.put(True)
            action()
            self.busy.put(False)
            status._finished(success=True)
        
        threading.Thread(target=run_and_wait, daemon=True).start()
        return status
    
    def pre_fly(self, max_frames=10000):
        """ """
        # reset the array counter
        self.detector.cam.array_counter.put(0)

        # enable the HDF5 plugin
        self.detector.hdf1.enable.put("Enable")
        
        # prepare to capture a stream of image frames in one array
        self.detector.hdf1.file_write_mode.put("Capture")
        
        # collect as many as this number
        self.detector.hdf1.num_capture.put(max_frames)
        
        # start to capture the stream
        self.detector.hdf1.capture.put("Capture")
    
    def post_fly(self):
        """ """
        # stream is now fully captured
        self.detector.hdf1.capture.put("Done")
        
        # write the HDF5 file
        self.detector.hdf1.write_file.put(1)
        
        # reset the HDF5 plugin to some default settings
        self.detector.hdf1.file_write_mode.put("Single")
        self.detector.hdf1.num_capture.put(1)
        self.detector.hdf1.enable.put("Disable")

    def kickoff(self):
        """
        Start a flyer
        """
        if self._completion_status is not None:
            raise RuntimeError("Already kicked off.")
        self._data = deque()

        self._future = self.loop.run_in_executor(None, self._spin_sequence)
        self._completion_status = DeviceStatus(device=self)
        return self._completion_status

    def _spin_sequence(self):
        """
        spin flyer, called from kickoff() in asyncio thread
        """
        self.return_position = self.motor.position
        
        for spin_num in range(self.num_spins):
            self.taxi()

            det_pre_acquire(self.detector)
            self.fly()
            det_post_acquire(self.detector)

            while self.detector.hdf1.write_file.value:
                time.sleep(0.01)    # wait for file to be written

            event = OrderedDict()
            event["time"] = time.time()
            event["seq_num"] = spin_num + 1
            event["data"] = {}
            event["timestamps"] = {}
            for d_item in (self.detector.hdf1.full_file_name,):
                d = d_item.read()
                for k, v in d.items():
                    event['data'][k] = v['value']
                    event['timestamps'][k] = v['timestamp']

            self._data.append(event)

        self.set("Return")
        self._completion_status._finished(success=True)

    def describe_collect(self):
        """
        Provide schema & meta-data from ``collect()``
        """
        dd = dict()
        dd.update(self.detector.hdf1.full_file_name.describe())
        return {self.stream_name: dd}

    def read_configuration(self):
        """
        """
        return OrderedDict()

    def describe_configuration(self):
        """
        """
        dd = dict()
        for obj in (self.motor, self.detector, self.busy): 
            dd.update(obj.describe_configuration())
        return OrderedDict()

    def complete(self):
        """
        Wait for flying to be complete
        """
        if self._completion_status is None:
            raise RuntimeError("No collection in progress")
        
        while self.motor.moving:
            time.sleep(self.poll_delay_s)
        
        return self._completion_status
    
    def collect(self):
        """
        Retrieve data from the flyer as *proto-events*
        """
        if self._completion_status is None or not self._completion_status.done:
            raise RuntimeError("No reading until done!")
        self._completion_status = None

        yield from self._data
    
    #def stop(self, *, success=False):
    #    """
    #    """
    #    pass


def myfly(flyers, *, md=None):
    """
    variant of bp.plans.fly() with stream-True on collect()
    """
    yield from bps.open_run(md)
    for flyer in flyers:
        yield from bps.kickoff(flyer, wait=True)
    for flyer in flyers:
        yield from bps.complete(flyer, wait=True)
    for flyer in flyers:
        yield from bps.collect(flyer, stream=True)
    yield from bps.close_run()


"""
USAGE:

    spin_flyer = SpinFlyer(m3, simdet, mybusy.state, name="spin_flyer")
    RE(bp.fly([spin_flyer]))
    RE(myfly([spin_flyer]))
"""
try:
    mybusy = BusyRecord("prj:mybusy", name="mybusy")
    spin_flyer = SpinFlyer(
        m3, simdet, mybusy.state,
        pre_start=-0.2, pos_start=-2.0, pos_finish=2.0,
        name="spin_flyer")
    setup_det_trigger(m3, simdet, calcs.calc3, calcs.calc4)
    # trigger image every motion modulo 0.25
    calcs.calc3.channels.B.value.put(0.25)
    # only enable detector triggers when HDF5 plugin is enabled
    calcs.calc4.channels.C.input_pv.put(simdet.hdf1.enable.pvname)
except Exception as _exc:
    print("problems setting up demo spin_flyer\n", _exc)


def example_planB():
    spin_flyer.pos_premove = spin_flyer.motor.position
    yield from mv(spin_flyer, "Taxi")
    yield from mv(spin_flyer, "Fly")
    yield from mv(spin_flyer, "Return")

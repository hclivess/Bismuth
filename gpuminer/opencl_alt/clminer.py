import options, time
import pyopencl as cl
import numpy as np
import threading, queue

# load config
config = options.Get()
config.read()

opencl_hash_count = config.opencl_hash_count_conf
opencl_timeout = config.opencl_timeout_conf
opencl_thread_multiplier = config.opencl_thread_multiplier_conf
opencl_disable_device = config.opencl_disable_device_conf
opencl_full_check = config.opencl_full_check_conf

# OpenCL classes
class oclResultQueue:
    def __init__(self):
        self.resultQueue_ = queue.Queue()

    def getNextCandidate(self, timeout):
        if timeout is not None and timeout <= 0:
            return None, 0
        try:
            candidate = self.resultQueue_.get( timeout=timeout )
            self.resultQueue_.task_done()
            return candidate, self.resultQueue_.qsize()
        except queue.Empty:
            return None, 0

    def pushCandidate(self, candidate):
        self.resultQueue_.put( candidate )

class oclDevice:
    def __init__(self, threadId, platId, devId, resultQueue):
        self.devId_ = devId
        self.platId_ = platId
        self.thread_ = None
        self.threadId_ = threadId
        self.threadCount_ = 0
        self.resultQueue_ = resultQueue

    def getName( self ):
        return self.device_.name

    def getThreadCount( self ):
        return self.threadCount_

    def setupCL( self, hash_count ):
        try:
            self.platform_ = cl.get_platforms()[ self.platId_ ]
            self.device_ = self.platform_.get_devices( device_type=cl.device_type.GPU )[ self.devId_ ]
            self.threadCount_ = opencl_thread_multiplier * self.device_.max_work_group_size
            self.hashCount_ = np.uint32( hash_count )

            print( "Setuping OpenCL miner for {}...".format( self.getName() ) )
            self.ctx_ = cl.Context( devices=[self.device_],
                                    properties=[(cl.context_properties.PLATFORM, self.platform_)] )
            assert self.ctx_ is not None

            self.queue_ = cl.CommandQueue( context=self.ctx_, device=self.device_ )
            assert self.queue_ is not None
            self.header_ = cl.Buffer( self.ctx_, size=56, flags=cl.mem_flags.READ_ONLY )
            assert self.header_ is not None
            self.tail_ = cl.Buffer( self.ctx_, size=56, flags=cl.mem_flags.READ_ONLY )
            assert self.tail_ is not None

            threadCount = self.threadCount_
            self.seed_ = cl.Buffer( self.ctx_, size=threadCount*8, flags=cl.mem_flags.READ_WRITE )
            assert self.seed_ is not None
            seed = np.random.bytes(threadCount*8)
            cl.enqueue_copy( self.queue_, src=seed, dest=self.seed_ )

            nonce0 = cl.Buffer( self.ctx_, size=self.retSize(), flags=cl.mem_flags.WRITE_ONLY )
            assert nonce0 is not None
            nonce1 = cl.Buffer( self.ctx_, size=self.retSize(), flags=cl.mem_flags.WRITE_ONLY )
            assert nonce1 is not None

            map0 = cl.Buffer( self.ctx_, size=self.retCount(), flags=cl.mem_flags.WRITE_ONLY )
            assert map0 is not None
            map1 = cl.Buffer( self.ctx_, size=self.retCount(), flags=cl.mem_flags.WRITE_ONLY )
            assert map1 is not None

            cnt0 = cl.Buffer( self.ctx_, size=4, flags=cl.mem_flags.READ_WRITE)
            assert cnt0 is not None
            cnt1 = cl.Buffer( self.ctx_, size=4, flags=cl.mem_flags.READ_WRITE)
            assert cnt1 is not None

            self.ret_ = [ nonce0, nonce1 ]
            self.retMap_ = [ map0, map1 ]
            self.retCnt_ = [ cnt0, cnt1 ]

            with open("opencl/bismuth.cl", "r") as clfile:
                source = clfile.read()

            self.program_ = cl.Program( self.ctx_, source )
            assert self.program_ is not None

            print( "Compiling OpenCL low diffculty miner for {}...".format( self.getName() ) )
            compileOp=[ "-cl-mad-enable", "-DHASH_COUNT={}".format(hash_count) ]
            if opencl_full_check != 0:
                compileOp.append( "-DBISMUTH_FULL_GPU_CHECK=1" )
            self.program_.build( options=compileOp, devices=[ self.device_ ] )
            #with open("bismuth.bin", "bw") as binfile:
            #    binfile.write( self.program_.binaries[0] )

            kernel0 = self.program_.bismuth
            assert kernel0 is not None
            kernel1 = self.program_.bismuth
            assert kernel1 is not None
            self.kernelLow_ = [ kernel0, kernel1 ]

            self.programHigh_ = cl.Program( self.ctx_, source )
            print( "Compiling OpenCL high diffculty miner for {}...".format( self.getName() ) )
            compileOp.append( "-DSEARCH_KEY_OVER_5=1" )
            self.programHigh_.build( options=compileOp, devices=[ self.device_ ] )

            kernelHi0 = self.programHigh_.bismuth
            assert kernelHi0 is not None
            kernelHi1 = self.programHigh_.bismuth
            assert kernelHi1 is not None
            self.kernelHigh_ = [ kernelHi0, kernelHi1 ]

            self.retNonces_ = np.zeros( 4*self.retCount(), dtype='u4' )

            self.retMaps_ = np.zeros( self.retCount(), dtype='B' )
        except Exception as e:
            print(e)
            raise

    def setKernelParams( self, key ):
        self.key_ = key

        self.kernelHigh_[0].set_args( self.header_, self.tail_,
                                      self.seed_, self.hashCount_, key,
                                      self.retCnt_[0],
                                      self.ret_[0], self.retMap_[0] )
        self.kernelHigh_[1].set_args( self.header_, self.tail_,
                                      self.seed_, self.hashCount_, key,
                                      self.retCnt_[1],
                                      self.ret_[1], self.retMap_[1] )

        self.kernelLow_[0].set_args( self.header_, self.tail_,
                                      self.seed_, self.hashCount_, key,
                                      self.retCnt_[0],
                                      self.ret_[0], self.retMap_[0] )
        self.kernelLow_[1].set_args( self.header_, self.tail_,
                                      self.seed_, self.hashCount_, key,
                                      self.retCnt_[1],
                                      self.ret_[1], self.retMap_[1] )

        self.kernel_ = self.kernelLow_ if key < 80 else self.kernelHigh_

    def setHeader(self, header):
        cl.enqueue_copy( self.queue_, src=header, dest=self.header_ )

    def setTail(self, tail):
        cl.enqueue_copy( self.queue_, src=tail, dest=self.tail_ )
        self.label_ = tail[:10].decode("utf-8")

    def readReturn( self, idx ):
        ev0 = cl.enqueue_copy( self.queue_, src=self.ret_[ idx ], dest=self.retNonces_, is_blocking=False )
        ev1 = cl.enqueue_copy( self.queue_, src=self.retMap_[ idx ], dest=self.retMaps_, is_blocking=False )
        cl.wait_for_events( [ ev0, ev1 ] )
        return self.retMaps_, self.retNonces_

    def readCount( self, idx, waitev ):
        ret = np.zeros( 1, dtype='u4' )
        readev = cl.enqueue_copy( self.queue_, src=self.retCnt_[ idx ], dest=ret, is_blocking=False, wait_for=[waitev])
        return readev, ret

    def runFirst( self, idx ):
        fillev = cl.enqueue_fill_buffer( self.queue_, self.retCnt_[ idx ],
                                         np.uint8(0), 0, 4 )
        runev = cl.enqueue_nd_range_kernel( self.queue_, self.kernel_[ idx ],
                                           (self.threadCount_,), None, wait_for=[fillev] )
        self.queue_.flush()
        return runev

    def run( self, idx, runev ):
        fillev = cl.enqueue_fill_buffer( self.queue_, self.retCnt_[ idx ],
                                         np.uint8(0), 0, 4 )
        runev = cl.enqueue_nd_range_kernel( self.queue_, self.kernel_[ idx ],
                                           (self.threadCount_,), None, wait_for=[fillev, runev] )
        self.queue_.flush()
        return runev

    def retCount(self):
        return self.threadCount_

    def hashCount(self):
        return self.hashCount_ * self.retCount()

    def retSize(self):
        return self.retCount() * 16

    #@profile
    def processLoop(self):
        loop = 0
        looptime = 0
        runev0 = self.runFirst( 0 )
        while self.running_:
            start = time.time()

            # While CPU checks hashes, GPU generate more
            # Finished ID0, so start ID1
            readev0, ret0 = self.readCount( 0, runev0 )
            runev1 = self.run( 1, runev0 )
            readev0.wait()
            if self.running_ == False:
                break

            if ret0[0] > 0:
                map, nonce = self.readReturn( 0 )
                index = map.nonzero()[0]
                for idx in index:
                    nidx = idx*4
                    #print( "(python) Found on index {}".format( nidx ) )
                    self.resultQueue_.pushCandidate( [nonce[ nidx: nidx+4 ].copy(),
                                                      self.threadId_] )

            # Finished ID1, so start ID0
            readev1, ret1 = self.readCount( 0, runev1 )
            runev0 = self.run( 0, runev1 )
            readev1.wait()
            if self.running_ == False:
                break

            if ret1[0] > 0:
                map, nonce = self.readReturn( 1 )
                index = map.nonzero()[0]
                for idx in index:
                    nidx = idx*4
                    #print( "(python) Found on index {}".format( nidx ) )
                    self.resultQueue_.pushCandidate( [nonce[ nidx: nidx+4 ].copy(),
                                                      self.threadId_] )

            end = time.time()
            looptime += end - start
            loop = loop + 1

            if (loop & 1) == 0:
                hashesCnt = loop * 2 * self.hashCount()
                cycles_per_second = hashesCnt/looptime
                print( "Thread{} {} @ {:,.4f} sec, {:,.2f} cycles/second, hashes: {:,}".format(
                    self.threadId_, self.label_, looptime, cycles_per_second, hashesCnt ) )
                loop = 0
                looptime = 0

    def startMining(self):
        if self.thread_ is None:
            self.running_ = True
            self.thread_ = threading.Thread( target=self.processLoop )
            self.thread_.start()

    def stopMining(self):
        self.running_ = False
        self.thread_.join()

class ocl:
    def __init__(self):
        self.devices_ = []

    def setupCL(self, resultQueue):
        platId = 0
        i = 0
        print( "Searching for OpenCL devices..." )
        platforms = cl.get_platforms()
        for plat in platforms:
            devId = 0
            devs = plat.get_devices( device_type=cl.device_type.GPU )
            for dev in devs:
                print( "Device {}: {} ({} threads)".format( i, dev.name, dev.max_work_group_size ) )
                self.devices_.append( oclDevice( i+1, platId, devId, resultQueue ) )
                i = i + 1
                devId = devId + 1
            platId = platId + 1

        if len(self.devices_) == 0:
            print( "No OpenCL devices found" )
            return

        print( "{} OpenCL devices found".format( len(self.devices_) ) )

    def getDevices(self):
        return [dev for dev in range(len(self.devices_)) if dev not in opencl_disable_device]

    def getMiners(self):
        instances = self.getDevices()
        miners = []
        for q in instances:
            m = self.getDevice(q)
            m.setupCL( opencl_hash_count )
            miners.append( m )
            print("thread " + str(q+1) + " started")
        return miners

    def getDevice(self, idx):
        return self.devices_[ idx ]

    def getTimeout(self):
        return opencl_timeout
# OpenCL classes

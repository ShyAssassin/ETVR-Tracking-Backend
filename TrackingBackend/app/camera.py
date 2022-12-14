from .logger import get_logger
from .config import CameraConfig
import cv2
import queue
import threading
from enum import Enum
logger = get_logger()


class CameraState(Enum):
    DISCONNECTED = 0
    CONNECTED = 1
    CONNECTING = 2


class Camera:
    def __init__(self, config: CameraConfig, image_queue: queue.Queue):
        self.config = config
        self.camera_state = CameraState.DISCONNECTED
        self.current_capture_source = self.config.capture_source
        self.camera: cv2.VideoCapture = None
        # Threading stuff
        self.cancellation_event: threading.Event = threading.Event()
        self.thread: threading.Thread = threading.Thread()  # cant be set to None because we call methods on it
        self.image_queue: queue.Queue = image_queue

    def start(self) -> None:
        # don't start a thread if one already exists
        if self.thread.is_alive():
            logger.debug(f"Thread requested to start but is already running")
            return

        logger.info("Starting Capture thread")
        # clear cancellation event incase thread was stopped in the past
        self.cancellation_event.clear()
        # We need to recreate the thread because it is not possible to start a thread that has already been stopped
        # MultiProcessing might have a way to do this in a better way????
        self.thread = threading.Thread(target=self.__run, name="Capture")
        self.thread.start()

    def stop(self) -> None:
        # can't kill a non-existent thread
        if not self.thread.is_alive():
            logger.debug("Request to kill dead thread was made!")
            return

        logger.info("Stopping Capture thread")
        self.cancellation_event.set()
        self.thread.join(timeout=5)
        self.camera.release()
        # If the thread fails to stop, start yelling at the top of your lungs and happy debugging!
        if self.thread.is_alive():
            logger.error(f"Failed to stop Capture thread!!!!!!!!")

    def restart(self) -> None:
        self.stop()
        self.start()

    def __run(self) -> None:
        while True:
            if self.cancellation_event.is_set():
                return
            # If things aren't open, retry until they are. Don't let read requests come in any earlier
            # than this, otherwise we can deadlock ourselves.
            if self.config.capture_source != "":
                if self.camera_state == CameraState.DISCONNECTED or self.current_capture_source != self.config.capture_source:
                    self.connect_camera()
                else:
                    self.get_camera_image()
            else:  # no capture source is defined yet, so we wait :3
                self.camera_state = CameraState.DISCONNECTED

    def connect_camera(self) -> None:
        try:
            self.camera_state = CameraState.CONNECTING
            self.current_capture_source = self.config.capture_source
            self.camera = cv2.VideoCapture(self.current_capture_source)
            self.camera.setExceptionMode(True)
            if self.camera.isOpened():
                self.camera_state = CameraState.CONNECTED
                logger.info("Camera connected!")
                return
            self.camera_state = CameraState.DISCONNECTED
            logger.info(f"Capture source {self.current_capture_source} not found, retrying")
        except (cv2.error, Exception):
            logger.exception("Something is very broken")

    def get_camera_image(self) -> None:
        # Be warned this is fucked beyond comprehension, if the capture source is dropped `self.camera.read()` wont
        # return for a very long time essentially soft lock the thread for around 30 seconds each time it is called
        # as far as I can tell our code is fine and that this is most likely a bug within OpenCV itself...
        # A dirty hack to fix this might be to just ping the host to see if it is alive before retrieving a new frame
        try:
            ret, frame = self.camera.read()
            if not ret:
                self.camera.set(cv2.CAP_PROP_POS_FRAMES, 0)
                logger.exception("Capture source problem, assuming camera disconnected, waiting for reconnect.")
                self.camera_state = CameraState.DISCONNECTED
                return
            frame_number = self.camera.get(cv2.CAP_PROP_POS_FRAMES)
            fps = self.camera.get(cv2.CAP_PROP_FPS)
            self.push_image_to_queue(frame, frame_number, fps)
        except (cv2.error, Exception):
            self.camera_state = CameraState.DISCONNECTED
            logger.exception("Failed to retrieve or push frame to queue")

    def push_image_to_queue(self, frame, frame_number, fps) -> None:
        # If there's backpressure, just yell. We really shouldn't have this unless we start getting
        # some sort of capture event conflict though.
        qsize = self.image_queue.qsize()
        if qsize > 1:
            logger.warning(f"CAPTURE QUEUE BACKPRESSURE OF {qsize}. CHECK FOR CRASH OR TIMING ISSUES IN ALGORITHM.")
        self.image_queue.put(frame, frame_number, fps)

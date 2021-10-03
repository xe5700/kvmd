# ========================================================================== #
#                                                                            #
#    KVMD - The main PiKVM daemon.                                           #
#                                                                            #
#    Copyright (C) 2018-2021  Maxim Devaev <mdevaev@gmail.com>               #
#                                                                            #
#    This program is free software: you can redistribute it and/or modify    #
#    it under the terms of the GNU General Public License as published by    #
#    the Free Software Foundation, either version 3 of the License, or       #
#    (at your option) any later version.                                     #
#                                                                            #
#    This program is distributed in the hope that it will be useful,         #
#    but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#    GNU General Public License for more details.                            #
#                                                                            #
#    You should have received a copy of the GNU General Public License       #
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.  #
#                                                                            #
# ========================================================================== #
#
# KVMD CH9329 Plugin - CH9329 HID plugin for KVMD based on serial plugin
# Copyright (C) 2021-2077 xe5700
# https://github.com/xe5700
#
#


import multiprocessing
import contextlib
import os
import queue
import struct
import time

from typing import Tuple, Any
from typing import List
from typing import Dict
from typing import Iterable
from typing import Generator
from typing import AsyncGenerator
from typing import Optional

from kvmd.plugins.hid.ch9329.proto import GET_INFO, make_with_sum, GET_INFO_S, check_with_sum, \
    CH9329_HEADER_S, ERRORS_REP_CODES, release_mouse, release_keyboard
from kvmd.logging import get_logger

from kvmd import tools
from kvmd import aiotools
from kvmd import aiomulti
from kvmd import aioproc

from kvmd.yamlconf import Option

from kvmd.validators.basic import valid_bool
from kvmd.validators.basic import valid_int_f0
from kvmd.validators.basic import valid_int_f1
from kvmd.validators.basic import valid_float_f01
from kvmd.validators.os import valid_abs_path
from kvmd.validators.hw import valid_gpio_pin_optional, valid_tty_speed

from kvmd.plugins.hid import BaseHid, serial

from .proto import BaseEvent
from .proto import KeyEvent
from .proto import MouseButtonEvent
from .proto import MouseMoveEvent
from .proto import MouseRelativeEvent
from .proto import MouseWheelEvent
import serial

# =====
from .._mcu import BasePhy, BasePhyConnection, BaseMcuHid, Gpio


class _RequestError(Exception):
    def __init__(self, msg: str) -> None:
        super().__init__(msg)
        self.msg = msg


class _PermRequestError(_RequestError):
    pass


class _TempRequestError(_RequestError):
    pass


# =====
class _CH3929PhyConnection(BasePhyConnection):
    def __init__(self, tty: serial.Serial) -> None:
        self.__tty = tty

    def send(self, request: bytes) -> bytes:
        assert request.startswith(CH9329_HEADER_S)

        if self.__tty.in_waiting:
            self.__tty.read_all()
        self.__tty.write(request)

        data = self.__tty.read(5)
        if len(data) < 5:
            raise _TempRequestError(f'error response data {data.hex(" ")}')
        _len = data[4]
        data += self.__tty.read(_len + 1)
        if len(data) - 6 != _len:
            raise _TempRequestError(f'error response data {data.hex(" ")} wrong length {len(data)}')
            pass
        return data


class _SerialPhy(BasePhy):
    def __init__(
        self,
        device_path: str,
        speed: int,
        read_timeout: float,
    ) -> None:
        self.__device_path = device_path
        self.__speed = speed
        self.__read_timeout = read_timeout

    def has_device(self) -> bool:
        return os.path.exists(self.__device_path)

    @contextlib.contextmanager
    def connected(self) -> Generator[_CH3929PhyConnection, None, None]:  # type: ignore
        with serial.Serial(self.__device_path, self.__speed, timeout=self.__read_timeout) as tty:
            yield _CH3929PhyConnection(tty)


class CH9329McuHid(BaseHid, multiprocessing.Process):  # pylint: disable=too-many-instance-attributes
    Lock_NumLock: bool = False
    Lock_CapsLock: bool = False
    Lock_ScrollLock: bool = False
    ConnectState: bool = False
    Version: str = False

    def __init__(  # pylint: disable=too-many-arguments,super-init-not-called
        self,
        phy: BasePhy,

        gpio_device_path: str,
        reset_pin: int,
        reset_inverted: bool,
        reset_delay: float,

        read_retries: int,
        common_retries: int,
        retries_delay: float,
        errors_threshold: int,
        noop: bool,
    ) -> None:

        multiprocessing.Process.__init__(self, daemon=True)

        self.__read_retries = read_retries
        self.__common_retries = common_retries
        self.__retries_delay = retries_delay
        self.__errors_threshold = errors_threshold
        self.__noop = noop

        self.__phy = phy
        self.__gpio = Gpio(gpio_device_path, reset_pin, reset_inverted, reset_delay)

        self.__reset_required_event = multiprocessing.Event()
        self.__events_queue: "multiprocessing.Queue[BaseEvent]" = multiprocessing.Queue()

        self.__notifier = aiomulti.AioProcessNotifier()
        self.__state_flags = aiomulti.AioSharedFlags({
            "online": 0,
            "busy": 0,
            "status": 0,
            "caps": 0,
            "num": 0,
            "scroll": 0
        }, self.__notifier, type=int)

        self.__stop_event = multiprocessing.Event()

    @classmethod
    def get_plugin_options(cls) -> Dict:
        return {
            "gpio_device": Option("/dev/gpiochip0", type=valid_abs_path, unpack_as="gpio_device_path"),
            "reset_pin": Option(-1, type=valid_gpio_pin_optional),
            "reset_inverted": Option(False, type=valid_bool),
            "reset_delay": Option(0.1, type=valid_float_f01),

            "read_retries": Option(5, type=valid_int_f1),
            "common_retries": Option(5, type=valid_int_f1),
            "retries_delay": Option(0.5, type=valid_float_f01),
            "errors_threshold": Option(5, type=valid_int_f0),
            "noop": Option(False, type=valid_bool),
        }

    def sysprep(self) -> None:
        get_logger(0).info("Starting HID daemon ...")
        self.start()

    async def get_state(self) -> Dict:
        state = await self.__state_flags.get()
        online = bool(state["online"])
        caps = bool(state["caps"])
        num = bool(state["num"])
        scroll = bool(state["scroll"])

        # pong = (state["status"] >> 16) & 0xFF
        # outputs1 = (state["status"] >> 8) & 0xFF
        # outputs2 = state["status"] & 0xFF

        absolute = True
        # active_mouse = get_active_mouse(outputs1)
        # if online and active_mouse in ["usb_rel", "ps2"]:
        #    absolute = False

        keyboard_outputs: Dict = {"available": [], "active": ""}
        mouse_outputs: Dict = {"available": [], "active": ""}

        return {
            "online": online,
            "busy": False,
            "connected": online,
            "keyboard": {
                "online": online,
                "leds": {
                    "caps": caps,
                    "scroll": scroll,
                    "num": num,
                },
                "outputs": keyboard_outputs,
            },
            "mouse": {
                "online": online,
                "absolute": absolute,
                "outputs": mouse_outputs,
            },
        }

    async def poll_state(self) -> AsyncGenerator[Dict, None]:
        prev_state: Dict = {}
        while True:
            state = await self.get_state()
            if state != prev_state:
                yield state
                prev_state = state
            await self.__notifier.wait()

    async def reset(self) -> None:
        self.__reset_required_event.set()

    @aiotools.atomic
    async def cleanup(self) -> None:
        if self.is_alive():
            get_logger(0).info("Stopping HID daemon ...")
            self.__stop_event.set()
        if self.is_alive() or self.exitcode is not None:
            self.join()

    # =====

    def send_key_events(self, keys: Iterable[Tuple[str, bool]]) -> None:
        for (key, state) in keys:
            self.__queue_event(KeyEvent(key, state))

    def send_mouse_button_event(self, button: str, state: bool) -> None:
        self.__queue_event(MouseButtonEvent(button, state))

    def send_mouse_move_event(self, to_x: int, to_y: int) -> None:
        # return
        self.__queue_event(MouseMoveEvent(to_x=to_x, to_y=to_y))

    def send_mouse_relative_event(self, delta_x: int, delta_y: int) -> None:
        self.__queue_event(MouseRelativeEvent(delta_x, delta_y))

    def send_mouse_wheel_event(self, delta_x: int, delta_y: int) -> None:
        self.__queue_event(MouseWheelEvent(delta_x, delta_y))

    def set_params(self, keyboard_output: Optional[str] = None, mouse_output: Optional[str] = None) -> None:
        events: List[BaseEvent] = []
        if keyboard_output is not None:
            self.__set_state_busy(True)
            # events.append(SetKeyboardOutputEvent(keyboard_output))
        if mouse_output is not None:
            self.__set_state_busy(True)
            # events.append(SetMouseOutputEvent(mouse_output))
        for (index, event) in enumerate(events, 1):
            self.__queue_event(event, clear=(index == len(events)))

    def set_connected(self, connected: bool) -> None:
        self.ConnectState = connected
        return

    def clear_events(self) -> None:
        pass

    def __queue_event(self, event: BaseEvent, clear: bool = False) -> None:
        if not self.__stop_event.is_set():
            if clear:
                # FIXME: Если очистка производится со стороны процесса хида, то возможна гонка между
                # очисткой и добавлением нового события. Неприятно, но не смертельно.
                # Починить блокировкой после перехода на асинхронные очереди.
                tools.clear_queue(self.__events_queue)
            self.__events_queue.put_nowait(event)

    def run(self) -> None:  # pylint: disable=too-many-branches
        logger = aioproc.settle("HID", "hid")
        while not self.__stop_event.is_set():
            try:
                with self.__gpio:
                    self.__hid_loop()
                    if self.__phy.has_device():
                        logger.info("Clearing HID events ...")
                        try:
                            with self.__phy.connected() as conn:
                                pass
                                # self.__process_request(conn, ClearEvent().make_request())
                        except Exception:
                            logger.exception("Can't clear HID events")
            except Exception:
                logger.exception("Unexpected error in the GPIO loop")
                time.sleep(1)

    def __hid_loop(self) -> None:
        while not self.__stop_event.is_set():
            try:
                if not self.__hid_loop_wait_device():
                    continue
                with self.__phy.connected() as conn:
                    self.__process_request(conn, GET_INFO_S)
                    while not (self.__stop_event.is_set() and self.__events_queue.qsize() == 0):
                        if self.__reset_required_event.is_set():
                            try:
                                self.__set_state_busy(True)
                                self.__gpio.reset()
                            finally:
                                self.__reset_required_event.clear()
                        try:
                            event = self.__events_queue.get(timeout=0.5)
                        except queue.Empty:
                            self.__process_request(conn, GET_INFO_S)
                        else:
                            if not self.__process_request(conn, event.make_down()):
                                self.clear_events()

            except Exception:
                self.clear_events()
                get_logger(0).exception("Unexpected error in the HID loop")
                time.sleep(1)

    def __hid_loop_wait_device(self) -> bool:
        logger = get_logger(0)
        logger.info("Initial HID reset and wait ...")
        self.__gpio.reset()
        # На самом деле SPI и Serial-девайсы не пропадают, просто резет и ожидание
        # логичнее всего делать именно здесь. Ну и на будущее, да
        for _ in range(10):
            if self.__phy.has_device():
                logger.info("HID found")
                return True
            if self.__stop_event.is_set():
                break
            time.sleep(1)
        logger.error("Missing HID")
        return False

    def __process_request(self, conn: BasePhyConnection, request: bytes) -> bool:  # pylint: disable=too-many-branches
        logger = get_logger()
        error_messages: List[str] = []
        live_log_errors = False

        common_retries = self.__common_retries
        read_retries = self.__read_retries
        error_retval = False

        while common_retries and read_retries:
            response: bytes
            if not self.__noop:
                response = conn.send(request)
            else:
                self.__set_state_online(True)
                break
            try:
                if not response.startswith(CH9329_HEADER_S) or len(response) < 4 or not check_with_sum(response):
                    conn.send(GET_INFO_S)
                    raise _TempRequestError(f"Invalid CH9329 response code; request={request.hex(' ')}; reponse"
                                            f"={response.hex(' ')}")
                _REQ_COMMAND_CODE = request[3]
                if _REQ_COMMAND_CODE == 0x87:
                    break
                elif len(response) < 4:
                    raise _TempRequestError(f"Invalid CH9329 response={response.hex(' ')} request={request.hex(' ')}")
                _REP_COMMAND_CODE = response[3]
                if _REP_COMMAND_CODE > 0xC0:
                    # 出现问题返回信息
                    if _REQ_COMMAND_CODE + 0xC0 == _REP_COMMAND_CODE:
                        code = response[5]
                        _repMsg = ERRORS_REP_CODES.get(str(code))
                        raise _TempRequestError(f"CH9329 error: {_repMsg} code: {code}")
                elif _REP_COMMAND_CODE > 0X80:
                    if _REQ_COMMAND_CODE + 0x80 == _REP_COMMAND_CODE:
                        if _REQ_COMMAND_CODE == 0x01:
                            _retV = int(response[5]) - 0x30
                            version = f'V1.{_retV}'
                            online = bool(response[6])
                            status = response[7]
                            _numL = bool(status & 1)
                            _capsL = bool(status & 2)
                            _scrollL = bool(status & 4)
                            self.__set_state_online(online)
                            self.__set_state_num_lock(_numL)
                            self.__set_state_caps_lock(_capsL)
                            self.__set_state_scroll_lock(_scrollL)
                            # print(f"版本:{version};  USB端:{online};  键盘 [NUM LOCK]:{_numL}  "
                            #      f"键盘 [CAPS LOCK]:{_capsL}  "
                            #      f"键盘 [SCROLL LOCK]:{_scrollL} \n {response.hex(' ')}")
                            break
                        elif _REQ_COMMAND_CODE < 0x10:
                            # print(f"返回代码 {_REP_COMMAND_CODE} 请求代码 {_REQ_COMMAND_CODE}")
                            break
                        else:
                            raise _TempRequestError(f"Not supported response={response.hex(' ')!r}")

                raise _TempRequestError(f"Invalid response from HID: request={request.hex(' ')!r}, response=0"
                                        f"x{response.hex(' ')!r}")

            except _RequestError as err:
                common_retries -= 1

                if live_log_errors:
                    logger.error(err.msg)
                else:
                    error_messages.append(err.msg)
                    if len(error_messages) > self.__errors_threshold:
                        for msg in error_messages:
                            logger.error(msg)
                        error_messages = []
                        live_log_errors = True

                if isinstance(err, _PermRequestError):
                    error_retval = True
                    break

                self.__set_state_online(False)

                if common_retries and read_retries:
                    time.sleep(self.__retries_delay)

        for msg in error_messages:
            logger.error(msg)
        if not (common_retries and read_retries):
            logger.error("Can't process HID request due many errors: %r", request)
        return error_retval

    def __set_state_online(self, online: bool) -> None:
        self.__state_flags.update(online=int(online))

    def __set_state_busy(self, busy: bool) -> None:
        self.__state_flags.update(busy=int(busy))

    def __set_state_num_lock(self, num: bool) -> None:
        self.__state_flags.update(num=int(num))

    def __set_state_caps_lock(self, caps: bool) -> None:
        self.__state_flags.update(caps=int(caps))

    def __set_state_scroll_lock(self, scroll: bool) -> None:
        self.__state_flags.update(scroll=int(scroll))


# =====
class Plugin(CH9329McuHid):
    def __init__(self, **kwargs: Any) -> None:
        phy_kwargs: Dict = {
            (option.unpack_as or key): kwargs.pop(option.unpack_as or key)
            for (key, option) in self.__get_phy_options().items()
        }
        super().__init__(phy=_SerialPhy(**phy_kwargs), **kwargs)

    @classmethod
    def get_plugin_options(cls) -> Dict:
        return {
            **cls.__get_phy_options(),
            **CH9329McuHid.get_plugin_options(),
        }

    @classmethod
    def __get_phy_options(cls) -> Dict:
        return {
            "device": Option("/dev/ttyUSB0", type=valid_abs_path, unpack_as="device_path"),
            "speed": Option(115200, type=valid_tty_speed),
            "read_timeout": Option(5.0, type=valid_float_f01),
        }

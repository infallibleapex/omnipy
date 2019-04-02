from .exceptions import ProtocolError, PacketRadioError
from podcomm import crc
from podcomm.protocol_common import *
from .packet_radio import TxPower
from .pr_rileylink import RileyLink
from .definitions import *
from threading import Thread, Event
import binascii
import time

def _ack_data(address1, address2, sequence):
    return RadioPacket(address1, RadioPacketType.ACK, sequence,
                     struct.pack(">I", address2));

class PdmRadio:
    pod_message: PodMessage

    def __init__(self, radio_address, msg_sequence=0, pkt_sequence=0, packet_radio=None):
        self.radio_address = radio_address
        self.message_sequence = msg_sequence
        self.packet_sequence = pkt_sequence
        self.last_received_packet = None
        self.logger = getLogger()
        self.packet_logger = get_packet_logger()
        self.message_logger = get_message_logger()

        if packet_radio is None:
            self.packet_radio = RileyLink()
        else:
            self.packet_radio = packet_radio

        self.last_packet_received = None
        self.last_packet_timestamp = None
        self.radio_ready = Event()
        self.request_arrived = Event()
        self.response_received = Event()
        self.request_shutdown = Event()
        self.request_message = None
        self.double_take = False
        self.tx_power = None
        self.pod_message = None
        self.response_exception = None
        self.radio_thread = Thread(target=self._radio_loop)
        self.radio_thread.setDaemon(True)
        self.radio_thread.start()

    def stop(self):
        self.radio_ready.wait()
        self.radio_ready.clear()
        self.request_shutdown.set()
        self.request_arrived.set()
        self.radio_thread.join()

    def send_message_get_message(self, message: PdmMessage,
                                 message_address=None,
                                 ack_address_override=None,
                                 tx_power=None, double_take=False):
        self.radio_ready.wait()
        self.radio_ready.clear()

        self.pdm_message = message
        if message_address is None:
            self.pdm_message_address = self.radio_address
        else:
            self.pdm_message_address = message_address
        self.ack_address_override = ack_address_override
        self.pod_message = None
        self.double_take = double_take
        self.tx_power = tx_power

        self.request_arrived.set()

        self.response_received.wait()
        self.response_received.clear()
        if self.pod_message is None:
            raise self.response_exception
        return self.pod_message

    def disconnect(self):
        try:
            self.packet_radio.disconnect(ignore_errors=True)
        except Exception:
            self.logger.exception("Error while disconnecting")

    def _radio_loop(self):
        while not self._radio_init():
            self.logger.warning("Failed to initialize radio, retrying")
            time.sleep(5)

        self.radio_ready.set()
        while True:
            if not self.request_arrived.wait(timeout=10.0):
                self.disconnect()
            self.request_arrived.wait()
            self.request_arrived.clear()

            if self.request_shutdown.wait(0):
                break

            try:
                self.pod_message = self._send_and_get(self.pdm_message, self.pdm_message_address,
                                                      self.ack_address_override,
                                                      tx_power=self.tx_power, double_take=self.double_take)
                self.response_exception = None
            except Exception as e:
                self.pod_message = None
                self.response_exception = e

            if self.response_exception is None:
                ack_packet = self._final_ack(self.ack_address_override, self.packet_sequence)
                self.packet_sequence = (self.packet_sequence + 1) % 32
                self.response_received.set()

                try:
                    self._send_packet(ack_packet)
                    self.logger.debug("Conversation ended")
                except Exception as e:
                    self.logger.exception("Error during ending conversation, ignored.")
            else:
                self.response_received.set()

            self.radio_ready.set()

    def _interim_ack(self, ack_address_override, sequence):
        if ack_address_override is None:
            return _ack_data(self.radio_address, self.radio_address, sequence)
        else:
            return _ack_data(self.radio_address, ack_address_override, sequence)

    def _final_ack(self, ack_address_override, sequence):
        if ack_address_override is None:
            return _ack_data(self.radio_address, 0, sequence)
        else:
            return _ack_data(self.radio_address, ack_address_override, sequence)

    def _radio_init(self, retries=1):
        retry = 0
        while retry < retries:
            try:
                self.disconnect()
                self.packet_radio.connect(force_initialize=True)
                return True
            except:
                self.logger.exception("Error during radio initialization")
                time.sleep(2)
                retry += 1
        return False

    def _send_and_get(self, pdm_message: PdmMessage, pdm_message_address, ack_address_override=None,
                      tx_power=None, double_take=False):

        if tx_power is not None:
            try:
                self.packet_radio.set_tx_power(tx_power)
            except PacketRadioError:
                if not self._radio_init(3):
                    raise

        packets = pdm_message.get_radio_packets(message_address=pdm_message_address,
                                                message_sequence=self.message_sequence,
                                                packet_address=self.radio_address,
                                                first_packet_sequence=self.packet_sequence)

        self.message_logger.info("SEND %s" % pdm_message)
        received = None

        if len(packets) > 1:
            if double_take:
                received = self._exchange_packets(packets[0].with_sequence(self.packet_sequence), RadioPacketType.ACK)
                self.packet_sequence = (received.sequence + 1) % 32

            received = self._exchange_packets(packets[0].with_sequence(self.packet_sequence), RadioPacketType.ACK)
            self.packet_sequence = (received.sequence + 1) % 32

            if len(packets) > 2:
                for packet in packets[1:-1]:
                    received = self._exchange_packets(packet, RadioPacketType.ACK)
                    self.packet_sequence = (received.sequence + 1) % 32

        received = self._exchange_packets(packets[-1].with_sequence(self.packet_sequence), RadioPacketType.POD)
        self.packet_sequence = (received.sequence + 1) % 32

        pod_response = PodMessage()
        while not pod_response.add_radio_packet(received):
            ack_packet = self._interim_ack(ack_address_override, (received.sequence + 1) % 32)
            received = self._exchange_packets(ack_packet, RadioPacketType.CON)

        self.message_logger.info("RECV %s" % pod_response)
        self.message_sequence = (pod_response.sequence + 1) % 16
        return pod_response


    def _exchange_packets(self, packet_to_send, expected_type, timeout=10):
        start_time = None
        while start_time is None or time.time() - start_time < timeout:
            try:
                if self.last_packet_timestamp is None or time.time() - self.last_packet_timestamp > 3000:
                    self._awaken()
                    self.last_packet_timestamp = time.time()
                received = self.packet_radio.send_and_receive_packet(packet_to_send.get_data(), 0, 0, 100, 1, 130)
                if start_time is None:
                    start_time = time.time()

                self.packet_logger.info("SEND %s" % packet_to_send)

                if received is None:
                    self.packet_logger.debug("Received nothing")
                    self.packet_radio.tx_up()
                    continue
                p, rssi = self._get_packet(received)
                if p is None:
                    self.packet_logger.debug("RECEIVED BAD DATA: %s" % received.hex())
                    self.packet_radio.tx_down()
                    continue

                self.packet_logger.info("RECV %s" % p)
                if p.address != self.radio_address:
                    self.packet_logger.debug("Received packet for another address (more than one pod active?)")
                    self.packet_radio.tx_down()
                    continue

                self.last_packet_timestamp = time.time()

                if expected_type is not None and p.type != expected_type:
                    if self.last_packet_received is not None:
                        if p.sequence == self.last_packet_received.sequence:
                            self.packet_logger.debug("Received previous response")
                            self.packet_radio.tx_up()
                            continue

                    self.packet_logger.debug("RECEIVED unexpected packet: %s" % p)
                    self.last_packet_received = p

                    if packet_to_send.type == RadioPacketType.PDM:
                        self.packet_sequence = (p.sequence + 1) % 32
                        packet_to_send.with_sequence(self.packet_sequence)
                        continue
                    else:
                        raise ProtocolError("Aborting message transmission")

                if p.sequence != (packet_to_send.sequence + 1) % 32:
                    self.packet_logger.debug("RECEIVED packet with unexpected sequence: %s" % p)
                    self.last_packet_received = p
                    if packet_to_send.type == RadioPacketType.PDM:
                        self.packet_sequence = (p.sequence + 1) % 32
                        packet_to_send.with_sequence(self.packet_sequence)
                        continue
                    else:
                        raise ProtocolError("Aborting message transmission")

                self.last_packet_received = p
                self.logger.debug("SEND AND RECEIVE complete")
                return p
            except PacketRadioError:
                self.logger.exception("Radio error during send and receive, retrying")
                if not self._radio_init(3):
                    raise
                start_time = time.time()
        else:
            raise TimeoutError("Exceeded timeout while send and receive")

    def _send_packet(self, packet_to_send, timeout=25):
        start_time = None
        while start_time is None or time.time() - start_time < timeout:
            try:
                self.packet_logger.info("SEND %s" % packet_to_send)

                received = self.packet_radio.send_and_receive_packet(packet_to_send.get_data(), 5, 55, 300, 2, 40)
                if start_time is None:
                    start_time = time.time()

                # if self.request_arrived.wait(timeout=0):
                #     self.logger.debug("Prematurely exiting final phase to process next request")
                #     return
                if received is None:
                    received = self.packet_radio.get_packet(1.0)
                    if received is None:
                        self.packet_logger.debug("Silence")
                        break
                p, rssi = self._get_packet(received)
                if p is None:
                    self.packet_logger.debug("RECEIVED BAD DATA: %s" % received.hex())
                    self.packet_radio.tx_down()
                    continue

                if p.address != self.radio_address:
                    self.packet_logger.debug("Received packet for another address (more than one pod active?)")
                    self.packet_radio.tx_down()
                    continue

                self.last_packet_timestamp = time.time()
                if self.last_packet_received is not None:
                    if p.type == self.last_packet_received.type and p.sequence == self.last_packet_received.sequence:
                        self.packet_logger.debug("Received previous response")
                        self.packet_radio.tx_up()
                        continue

                self.packet_logger.info("RECV %s" % p)
                self.packet_logger.debug("RECEIVED unexpected packet: %s" % p)
                self.last_packet_received = p
                self.packet_sequence = (p.sequence + 1) % 32
                packet_to_send.with_sequence(self.packet_sequence)
                continue


            except PacketRadioError:
                self.logger.exception("Radio error during send and receive, retrying")
                if not self._radio_init(3):
                    raise
                start_time = time.time()
        else:
            self.logger.warning("Exceeded timeout while waiting for silence to fall")

    def _get_packet(self, data):
        rssi = None
        if data is not None and len(data) > 2:
            rssi = data[0]
            try:
                return RadioPacket.parse(data[2:]), rssi
            except:
                getLogger().exception("RECEIVED DATA: %s RSSI: %d" % (binascii.hexlify(data[2:]), rssi))
        return None, rssi

    def _awaken(self):
        self.packet_radio.send_packet(bytes(), 0, 0, 250)
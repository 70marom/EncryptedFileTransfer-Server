import os.path
import struct
from Crypto.Random import get_random_bytes
from protocol_handler import failed_register, success_register, success_login, failed_login, send_aes_key, \
    general_error, send_file_crc, send_final_confirmation
from util import string_to_uuid
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP, AES
from cksum import file_crc
from codes import RequestCode

class Client:
    def __init__(self, connection, address, database):
        self.connection = connection
        self.address = address
        self.database = database
        self.client_id = None
        self.running = True
        self.aes_key = None
        self.name = None
        self.received_packets = 0

    def get_requests(self):
        while self.running: # keep receiving requests until the client disconnects or an error occurs
            try:
                header = self.connection.recv(23) # 16 bytes for client_id, 1 byte for version, 2 bytes for code, 4 bytes for payload size
                if not header:
                    self.running = False
                    return
                if len(header) != 23:
                    raise ValueError()
                # unpack the header in little endian format
                self.client_id, version, code, payload_size = struct.unpack('<16sBHI', header)
                payload = self.connection.recv(payload_size) # receive the payload
            except (struct.error, ConnectionError, ValueError):
                print(f"Error: failed to receive data from {self.address}.")
                self.running = False
                return
            match code:
                case RequestCode.REGISTRATION_REQUEST:
                    self.handle_register(payload)
                case RequestCode.RECEIVE_PUBLIC_KEY:
                    self.handle_public_key(payload)
                case RequestCode.LOGIN_REQUEST:
                    self.handle_login(payload)
                case RequestCode.SAVE_FILE_REQUEST:
                    self.handle_save_file(payload)
                case RequestCode.FILE_TRANSFER_SUCCESS:
                    self.handle_transfer_success(payload)
                case RequestCode.CRC_MISMATCH:
                    print(f"Error: {self.address} got a different CRC for {payload.decode().strip('\x00')}. Receiving file again.")
                    continue
                case RequestCode.FILE_TRANSFER_FAILED:
                    self.handle_transfer_failed(payload)
                case _: # default case, handle unknown request codes
                    print(f"Received unknown request code {code} from {self.address}")
                    general_error().send(self.connection)
                    self.running = False

    def handle_register(self, payload):
        client_exists = self.database.client_exists(payload)
        if client_exists is None:
            general_error().send(self.connection)
            return
        if client_exists:
            print(f"{self.address} tried to register with the name {payload.decode()[:-1]}, but it's already taken.")
            failed_register().send(self.connection)
            self.running = False
        else:
            self.client_id = bytes.fromhex(string_to_uuid(payload.decode()))
            if not self.database.register_client(self.client_id, payload.decode()):
                general_error().send(self.connection)
                return
            print(f"{self.address} registered with the name {payload.decode()[:-1]}")
            self.name = payload.decode()[:-1]
            success_register(self.client_id).send(self.connection)

    def handle_login(self, payload):
        client_exists = self.database.client_exists_by_id(self.client_id, payload.decode())
        if client_exists is None:
            general_error().send(self.connection)
            return
        if client_exists:
            print(f"{self.address} logged in with the name {payload.decode()[:-1]}")
            self.name = payload.decode()[:-1]
            if not self.generate_aes_key():
                return
            aes_key = self.database.get_aes_key(self.client_id)
            if aes_key is None:
                general_error().send(self.connection)
                return
            success_login(self.client_id, aes_key).send(self.connection)
            self.database.update_last_seen(self.client_id)
            print(f"Sent AES key to {self.address}.")
        else:
            print(f"{self.address} tried to log in with the name {payload.decode()[:-1]}, but it's not registered " +
                  "or its UUID is not valid.")
            failed_login(self.client_id).send(self.connection)

    def handle_public_key(self, payload):
        public_key = payload[255:]
        if not self.database.add_public_key(self.client_id, public_key):
            general_error().send(self.connection)
            return
        print(f"{self.address} sent its public key.")
        if not self.generate_aes_key():
            return
        aes_key = self.database.get_aes_key(self.client_id)
        if aes_key is None:
            general_error().send(self.connection)
            return
        send_aes_key(self.client_id, aes_key).send(self.connection)
        print(f"Sent AES key to {self.address}.")

    def generate_aes_key(self):
        public_key = self.database.get_public_key(self.client_id)
        if public_key is None:
            general_error().send(self.connection)
            return False
        public_key = RSA.importKey(public_key)
        aes_key = get_random_bytes(32)
        self.aes_key = aes_key
        rsa_cipher = PKCS1_OAEP.new(public_key)
        encrypted_aes_key = rsa_cipher.encrypt(aes_key)
        if not self.database.add_aes_key(self.client_id, encrypted_aes_key):
            general_error().send(self.connection)
            return False
        print(f"Generated a new AES key for {self.address} and encrypted it with the client's public key.")
        return True

    def handle_save_file(self, payload):
        try:
            content_size, decrypted_size, packet_number, total_packets, file_name = struct.unpack("<IIHH255s", payload[:267])
            file_name = file_name.decode().strip('\x00')
            file_path = os.path.join(self.name, file_name)
            encrypted_data = payload[267:267 + 1024]

            if not os.path.exists(self.name):
                os.makedirs(self.name)
                print(f"Created a directory for {self.name}.")

            if self.received_packets == 0:
                print(f"Receiving file {file_name} from {self.address}, expecting {total_packets} packets.")

            decrypted_data = self.decrypt_data(encrypted_data)

            if packet_number == total_packets:
                decrypted_data = decrypted_data.rstrip(b"\x00")

            if self.received_packets == 0 and os.path.exists(file_path):
                os.remove(file_path)

            with open(file_path, 'ab') as file:
                file.write(decrypted_data)
                print(f"Received packet {packet_number}/{total_packets} of {file_name} from {self.address}.")

            self.received_packets += 1

            if self.received_packets == total_packets:
                print(f"Received all packets for {file_name} from {self.address}.")
                self.received_packets = 0
                self.handle_file_crc(file_path, file_name, content_size)

        except (IOError, OSError, struct.error, FileNotFoundError):
            print(f"Error: failed to save file from {self.address}.")
            general_error().send(self.connection)
            self.running = False

    def decrypt_data(self, data):
        iv = b"\x00" * 16
        cipher = AES.new(self.aes_key, AES.MODE_CBC, iv)
        return cipher.decrypt(data)

    def handle_file_crc(self, file_path, file_name, content_size):
        crc = file_crc(file_path)
        if crc is None:
            general_error().send(self.connection)
            self.running = False
            return
        send_file_crc(self.client_id, content_size, file_name, crc).send(self.connection)
        print(f"Sent CRC for {file_name} to {self.address}.")

    def handle_transfer_success(self, payload):
        file_name = payload.decode().strip('\x00')
        if not self.database.save_file(self.client_id, file_name, os.path.join(self.name, file_name)):
            general_error().send(self.connection)
            return
        print(f"{self.address} successfully received {file_name} with matching CRC.")
        send_final_confirmation(self.client_id).send(self.connection)
        print(f"Sent final confirmation to {self.address} in order to close connection.")
        self.running = False

    def handle_transfer_failed(self, payload):
        file_name = payload.decode().strip('\x00')
        print(f"CRC for {file_name} from {self.address} did not match after 4 tries. Transfer failed.")
        file_path = os.path.join(self.name, file_name)
        if os.path.exists(file_path):
            os.remove(file_path)
        if not os.listdir(self.name):
            os.rmdir(self.name)
        if not self.database.save_file(self.client_id, file_name, file_path, False):
            general_error().send(self.connection)
            return
        send_final_confirmation(self.client_id).send(self.connection)
        print(f"Sent final confirmation to {self.address} in order to close connection.")
        self.running = False

# -*- coding: utf-8 -*-
"""
Copyright (c) 2015 Jesse Peterson
Licensed under the MIT license. See the included LICENSE.txt file for details.

Attributes:
    db (SQLAlchemy): A reference to flask SQLAlchemy extensions db instance.
"""
from flask_sqlalchemy import SQLAlchemy

import datetime
from enum import Enum
from sqlalchemy import Column, Integer, String, ForeignKey, Table, Text, Boolean, DateTime, Enum as DBEnum, text, \
    BigInteger, and_, or_, LargeBinary
from sqlalchemy.orm import relationship
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.ext.hybrid import hybrid_property
from .mutablelist import MutableList
from .dbtypes import GUID, JSONEncodedDict
from .mdm import CommandStatus, Platform, commands
import base64
from binascii import hexlify
from biplist import Data as NSData, readPlistFromString
from uuid import uuid4
from .profiles.cert import KeyUsage
from .profiles import PayloadScope

db = SQLAlchemy()


class CertificateType(Enum):
    """A list of the polymorphic identities available for subclassess of Certificate."""
    CSR = 'csr'
    PUSH = 'mdm.pushcert'
    WEB = 'mdm.webcrt'
    CA = 'mdm.cacert'
    DEVICE = 'mdm.device'


class Certificate(db.Model):
    """Polymorphic base for certificate types.
    
    These certificate classes are only intended to be used for storing certificates related to running the MDM or
    certificates issued by the MDM internal CA or SCEP service.
    
    Note that X.509 name attributes have fixed lengths as defined in `RFC5280`_. 
    
    :table: certificates
    
    Attributes:
          id (int): Primary Key
          pem_data (str): PEM Encoded Certificate Data
          rsa_private_key_id (int): Foreign key reference to an RSAPrivateKey IF the private key was generated by us.
          
          x509_cn (str): X.509 Common Name
          x509_ou (str): X.509 Organizational Unit
          x509_o (str): X.509 Organization
          x509_c (str): X.509 2 letter Country Code
          x509_st (str): X.509 State or Location
          
          not_before (datetime): Certificate validity - not before
          not_after (datetime): Certificate validity - not after
          fingerprint (str): SHA-256 hash of certificate
          push_topic (str): Only present for Push Certificates, the x.509 User ID field value
          discriminator (str): The polymorphic identity, used for subclasses.
           
    .. _RFC5280:
       http://www.ietf.org/rfc/rfc5280.txt 
    """
    __tablename__ = 'certificates'

    id = Column(Integer, primary_key=True)
    pem_data = Column(Text, nullable=False)
    rsa_private_key_id = Column(Integer, ForeignKey('rsa_private_keys.id'))

    x509_cn = Column(String(64), nullable=True)
    x509_ou = Column(String(32))
    x509_o = Column(String(64))
    x509_c = Column(String(2))
    x509_st = Column(String(128))

    not_before = Column(DateTime(timezone=False), nullable=False)
    not_after = Column(DateTime(timezone=False), nullable=False)
    # SHA-256 hash of DER-encoded certificate
    fingerprint = Column(String(64), nullable=False, index=True, unique=True)  # Unique

    push_topic = Column(String, nullable=True)  # Only required for push certificate

    discriminator = Column(String(20))

    __mapper_args__ = {
        'polymorphic_on': discriminator,
        'polymorphic_identity': 'certificates',
    }


class RSAPrivateKey(db.Model):
    """RSA Private Key Model"""
    __tablename__ = 'rsa_private_keys'

    #: id column
    id = Column(Integer, primary_key=True)
    pem_data = Column(Text, nullable=False)

    certificates = db.relationship(
        'Certificate',
        backref='rsa_private_key',
        lazy='dynamic'
    )


class CertificateSigningRequest(Certificate):
    """Polymorphic single table inheritance specifically for Certificate Signing Requests."""
    __mapper_args__ = {
        'polymorphic_identity': CertificateType.CSR.value
    }


class SSLCertificate(Certificate):
    """Polymorphic single table inheritance specifically for SSL certificates assigned to the MDM for HTTPS traffic."""
    __mapper_args__ = {
        'polymorphic_identity': CertificateType.WEB.value
    }


class PushCertificate(Certificate):
    """Polymorphic single table inheritance specifically for APNS MDM Push Certificates assigned to the MDM."""
    __mapper_args__ = {
        'polymorphic_identity': CertificateType.PUSH.value
    }


class CACertificate(Certificate):
    """Polymorphic single table inheritance specifically for Certificate Authorities generated by this MDM."""
    __mapper_args__ = {
        'polymorphic_identity': CertificateType.CA.value
    }


class DeviceIdentityCertificate(Certificate):
    """Polymorphic single table inheritance specifically for device identity certificates."""
    __mapper_args__ = {
        'polymorphic_identity': CertificateType.DEVICE.value
    }


class InternalCA(db.Model):
    """The InternalCA model keeps track of the issued certificate serial numbers."""
    __tablename__ = 'internal_ca'

    id = Column(Integer, primary_key=True)
    ca_type = Column(String(64), nullable=False, index=True)
    serial = Column(Integer, nullable=False)

    certificate_id = Column(ForeignKey('certificates.id'), unique=True)
    certificate = relationship('Certificate', backref='certificate_authority')

    def get_next_serial(self):
        '''Increment our serial number and return it for use in a 
        new certificate'''

        # MAX(serial) + 1
        pass


class Device(db.Model):
    """An enrolled device.
    
    Attributes:
          id (int):
          udid (str): Unique Device Identifier
          topic (str): The APNS topic the device is listening on.
          last_seen (datetime.datetime): When the device last contacted the MDM.
          is_enrolled (bool): Whether the MDM should consider this device enrolled.
          build_version (str): DeviceInformation BuildVersion
          device_name (str): Name of the device
          model (str): Name of the hardware model
          model_name (str): Longer name of the hardware model
          os_version (str): The operating system version number.
          product_name (str): The base product name of the hardware
          serial_number (str): The hardware serial number
          awaiting_configuration (bool): True if device is waiting at Setup Assistant
          push_magic (str): The UUID that establishes a unique relationship between the device and the MDM.
          token (str): The hex string representing the Device Token, required to push with APNS.
          last_push_at (datetime.datetime): The datetime when the last push was sent to APNS for this device.
          last_apns_id (str): The UUID of the last apns command sent.
          certificate_id (int): The ID of the certificate that this device is using to authenticate itself. May be null
            
    """
    __tablename__ = 'devices'

    # Common attributes
    id = Column(Integer, primary_key=True)
    udid = Column(String, index=True, nullable=True)
    topic = Column(String, nullable=True)
    last_seen = Column(DateTime, nullable=True)
    is_enrolled = Column(Boolean, default=False)

    # DeviceInformation that is optionally given in `Authenticate` message for a device
    build_version = Column(String)
    device_name = Column(String)
    model = Column(String)
    model_name = Column(String)
    os_version = Column(String)
    product_name = Column(String)
    serial_number = Column(String(64), index=True, nullable=True)

    # DeviceInformation extracted from replies
    hostname = Column(String, nullable=True)
    local_hostname = Column(String, nullable=True)

    available_device_capacity = Column(BigInteger, nullable=True)
    device_capacity = Column(BigInteger, nullable=True)

    wifi_mac = Column(String, nullable=True)
    bluetooth_mac = Column(String, nullable=True)

    # APNS / TokenUpdate
    awaiting_configuration = Column(Boolean, default=False)
    push_magic = Column(String, nullable=True)

    # The APNS device token is stored in base64 format. Descriptors are added to handle this encoding and decoding
    # to bytes automatically.
    _token = Column(String, nullable=True)

    @hybrid_property
    def token(self):
        return self._token if self._token is None else base64.b64decode(self._token)

    @token.setter
    def token(self, value):
        self._token = base64.b64encode(value) if value is not None else None

    @property
    def hex_token(self):
        """Retrieve the device token in hex encoding, necessary for the APNS2 client."""
        if self._token is None:
            return self._token
        else:
            return hexlify(self.token).decode('utf8')

    # if null there are no outstanding push notifications. If this contains anything then dont attempt to deliver
    # another APNS push.
    last_push_at = Column(DateTime, nullable=True)
    last_apns_id = Column(Integer, nullable=True)

    # if the time delta between last_push_at and last_seen is >= several days to a week,
    # this should count as a failed push, and potentially declare the device as dead.
    failed_push_count = Column(Integer, default=0, nullable=False)

    # DEP
    _unlock_token = Column(String(), name='unlock_token', nullable=True)

    dep_json = Column(MutableDict.as_mutable(JSONEncodedDict), nullable=True)
    dep_config_id = Column(ForeignKey('dep_config.id'), nullable=True)
    dep_config = relationship('DEPConfig', backref='devices')
    info_json = Column(MutableDict.as_mutable(JSONEncodedDict), nullable=True)
    first_user_message_seen = Column(Boolean, nullable=False, default=False)

    certificate_id = Column(Integer, ForeignKey('certificates.id'))
    certificate = relationship('Certificate', backref='devices')

    @property
    def unlock_token(self):
        return self._unlock_token

    @unlock_token.setter
    def unlock_token(self, value):
        if isinstance(value, NSData):
            self._unlock_token = NSData.encode('base64')
        else:
            self._unlock_token = value

    @property
    def platform(self) -> Platform:
        if self.model_name in ['iMac']:  # TODO: obviously not sufficient
            return Platform.macOS
        else:
            return Platform.iOS

    def __repr__(self):
        return '<Device ID=%r UDID=%r SerialNo=%r>' % (self.id, self.udid, self.serial_number)


class InstalledApplication(db.Model):
    """This model represents a single application that was returned as part of an ``InstalledApplicationList`` query.
    
    It is impossible to create a composite key to uniquely identify each row, therefore every time the device reports
    back we need to wipe all rows associated with a single device. The reason why a composite key won't work here is
    that macOS will often report the binary name and no identifier, version, or size (and sometimes iOS can do the
    inverse of that).
    
    :table: installed_applications
    """
    __tablename__ = 'installed_applications'

    id = Column(Integer, primary_key=True)
    device_udid = Column(GUID, index=True, nullable=False)
    device_id = Column(ForeignKey('devices.id'), nullable=True)
    device = relationship('Device', backref='installed_applications')

    # Many of these can be empty, so there is no valid composite key
    bundle_identifier = Column(String, index=True)
    version = Column(String, index=True)
    short_version = Column(String)
    name = Column(String)
    bundle_size = Column(BigInteger)
    dynamic_size = Column(BigInteger)
    is_validated = Column(Boolean)


class InstalledCertificate(db.Model):
    """This model represents a single installed certificate on an enrolled device as returned by the ``CertificateList``
    query.
    
    The response will usually include both certificates managed by profiles and certificates that were installed
    outside of a profile.
    
    :table: installed_certificates
    """
    __tablename__ = 'installed_certificates'

    id = Column(Integer, primary_key=True)
    device_udid = Column(GUID, index=True, nullable=False)
    device_id = Column(ForeignKey('devices.id'), nullable=True)
    device = relationship('Device', backref='installed_certificates')

    x509_cn = Column(String)
    is_identity = Column(Boolean)
    der_data = Column(LargeBinary, nullable=False)
    
    # SHA-256 hash of DER-encoded certificate
    fingerprint_sha256 = Column(String(64), nullable=False, index=True)


# class InstalledProfile(db.Model):
#     __tablename__ = 'installed_profiles'
#
#

class CommandSequence(db.Model):
    """A command sequence represents a series of commands where all members must succeed in order for the sequence to
    succeed. I.E a single failure or timeout in the sequence stops the delivery of every other member.

    :table: command_sequences
    """
    __tablename__ = 'command_sequences'

    id = Column(Integer, primary_key=True)
    

class Command(db.Model):
    """The command model represents a single MDM command that should be, has been, or has failed to be delivered to
    a single enrolled device.
    
    :table: commands
    
    Attributes:
        id (int): ID
        request_type (str): The command RequestType attribute
        uuid (GUID): Globally unique command UUID
        parameters (str): The parameters that were used when generating the command, serialized into JSON. Omitting the
            RequestType and CommandUUID attributes.
        status (CommandStatus): The status of the command.
        queued_at (datetime.datetime): The datetime (utc) of when the command was created. Defaults to UTC now
        sent_at (datetime.datetime): The datetime (utc) of when the command was delivered to the client.
        acknowledged_at (datetime.datetime): The datetime (utc) of when the Acknowledged, Error or NotNow response was
            returned.
        after (datetime.datetime): If not null, the command must not be sent until this datetime is in the past.
        ttl (int): The number of retries remaining until the command will be dead/expired.
        device_id (int): The device ID on the devices table.
        device (Device): The instance of the related device.
    """
    __tablename__ = 'commands'

    id = Column(Integer, primary_key=True)

    request_type = Column(String, nullable=False)  # string representation of our local command handler
    # request_type = Column(String, index=True, nullable=False) # actual command name
    uuid = Column(GUID, index=True, unique=True, nullable=False)
    parameters = Column(MutableDict.as_mutable(JSONEncodedDict),
                        nullable=True)  # JSON add'l data as input to command builder
    status = Column(String(1), index=True, nullable=False, default=CommandStatus.Queued.value)

    queued_at = Column(DateTime, default=datetime.datetime.utcnow(), server_default=text('CURRENT_TIMESTAMP'))
    sent_at = Column(DateTime, nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)

    # command must only be sent after this date
    after = Column(DateTime, nullable=True)

    # number of retries remaining until dead
    ttl = Column(Integer, nullable=False, default=5)

    device_id = Column(ForeignKey('devices.id'), nullable=True)
    device = relationship('Device', backref='commands')

    # device_user_id = Column(ForeignKey('device_users.id'), nullable=True)
    # device_user = relationship('DeviceUser', backref='commands')
    @classmethod
    def from_model(cls, cmd: commands.Command):
        c = cls()
        c.request_type = cmd.request_type
        c.uuid = cmd.uuid
        c.parameters = cmd.parameters

        return c

    @classmethod
    def find_by_uuid(cls, uuid):
        """Find and return an instance of the Command model matching the given UUID string.
        
        Args:
              uuid (str): The command UUID
              
        Returns:
              Command: Instance of the command, if any
        """
        return cls.query.filter(cls.uuid == uuid).one()

    @classmethod
    def get_next_device_command(cls, device):
        # d == d AND (q_status == Q OR (q_status == R AND result == 'NotNow'))
        return cls.query.filter(and_(
                cls.device == device,
                cls.status == CommandStatus.Queued.value)).order_by(cls.id).first()

    def __repr__(self):
        return '<QueuedCommand ID=%r UUID=%r qstatus=%r>' % (self.id, self.uuid, self.status)


class App(db.Model):
    __tablename__ = 'app'

    id = Column(Integer, primary_key=True)

    filename = Column(String, nullable=False, unique=True)
    filesize = Column(Integer, nullable=False)

    md5_hash = Column(String(32), nullable=False)  # MD5 hash of the entire file

    # MDM clients support a chunked method of retrival of the download file
    # presumably to best support OTA download of large updates. These fields
    # are in support of that mechanism
    md5_chunk_size = Column(Integer, nullable=False)
    md5_chunk_hashes = Column(Text, nullable=True)  # colon (:) separated list of MD5 chunk hashes

    bundle_ids_json = Column(MutableList.as_mutable(JSONEncodedDict), nullable=True)
    pkg_ids_json = Column(MutableList.as_mutable(JSONEncodedDict), nullable=True)

    def path_format(self):
        return '%010d.dat' % self.id

    def __repr__(self):
        return '<App ID=%r Filename=%r>' % (self.id, self.filename)


class DEPConfig(db.Model):
    __tablename__ = 'dep_config'

    id = Column(Integer, primary_key=True)

    # certificate for PKI of server token
    certificate_id = Column(ForeignKey('certificates.id'))
    certificate = relationship('Certificate', backref='dep_configs')

    server_token = Column(MutableDict.as_mutable(JSONEncodedDict), nullable=True)
    auth_session_token = Column(String, nullable=True)

    initial_fetch_complete = Column(Boolean, nullable=False, default=False)
    next_check = Column(DateTime(timezone=False), nullable=True)
    device_cursor = Column(String)
    device_cursor_recevied = Column(DateTime(timezone=False), nullable=True)  # shouldn't use if more than 7 days old

    url_base = Column(String, nullable=True)  # testing server environment if used

    def last_check_delta(self):
        if self.next_check:
            return str(self.next_check - datetime.datetime.utcnow())
        else:
            return ''

#
# class DEPProfile(db.Model):
#     __tablename__ = 'dep_profile'
#
#     id = Column(Integer, primary_key=True)
#
#     mdm_config_id = Column(ForeignKey('mdm_config.id'), nullable=False)
#     mdm_config = relationship('MDMConfig', backref='dep_profiles')
#
#     dep_config_id = Column(ForeignKey('dep_config.id'), nullable=False)
#     dep_config = relationship('DEPConfig', backref='dep_profiles')
#
#     # DEP-assigned UUID for this DEP profile
#     uuid = Column(String(36), index=True, nullable=True)  # should be unique but it's assigned to us so can't be null
#
#     profile_data = Column(MutableDict.as_mutable(JSONEncodedDict), nullable=False)
#
#     def profile_name(self):
#         return self.profile_data['profile_name']


class User(db.Model):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    name = Column(String)
    fullname = Column(String)
    password = Column(String)


class DeviceUser(db.Model):
    """
    This model represents a managed user from the standpoint of the MDM.
    It exists to support the macOS user channel extension.

    :table: device_users

    Attributes:
          user_id (GUID): Local user's GUID, or network user's GUID from Open Directory Record.
    """
    __tablename__ = 'device_users'

    id = Column(Integer, primary_key=True)

    udid = Column(GUID, nullable=False)
    user_id = Column(GUID, nullable=False)
    long_name = Column(String)
    short_name = Column(String)
    need_sync_response = Column(Boolean)  # This is kind of transitive but added anyway.
    user_configuration = Column(Boolean)
    digest_challenge = Column(String)
    auth_token = Column(String)


class Organization(db.Model):
    __tablename__ = 'organizations'

    id = Column(Integer, primary_key=True)
    name = Column(String)
    payload_prefix = Column(String)

    # http://www.ietf.org/rfc/rfc5280.txt
    # maximum string lengths are well defined by this RFC and this schema follows those recommendations
    # this x.509 name is used in the subject of the internal CA and issued certificates
    x509_ou = Column(String(32))
    x509_o = Column(String(64))
    x509_st = Column(String(128))
    x509_c = Column(String(2))


class AppSourceType(Enum):
    S3 = 'S3'
    Munki = 'Munki'


class AppSource(db.Model):
    __tablename__ = 'app_sources'

    id = Column(Integer, primary_key=True)
    name = Column(String)
    source_type = Column(DBEnum(AppSourceType), default=AppSourceType.Munki)


class SCEPConfig(db.Model):
    __tablename__ = 'scep_config'

    id = Column(Integer, primary_key=True)
    url = Column(String, nullable=False)

    challenge_enabled = Column(Boolean, default=False)
    challenge = Column(String)
    ca_fingerprint = Column(String)
    subject = Column(String, nullable=False)  # eg. O=x/OU=y/CN=z
    key_size = Column(Integer, default=2048, nullable=False)
    key_type = Column(String, default='RSA', nullable=False)
    key_usage = Column(DBEnum(KeyUsage), default=KeyUsage.All)
    subject_alt_name = Column(String, nullable=True)
    retries = Column(Integer, default=3, nullable=False)
    retry_delay = Column(Integer, default=10, nullable=False)
    certificate_renewal_time_interval = Column(Integer, default=14, nullable=False)
    

# -*- coding: utf-8 -*-

# Copyright (c) 2013-2019 NASK. All rights reserved.

"""
Low-level details of certificate generation (OpenSSL-based).
"""

import datetime
import os
import os.path as osp
import shutil
import tempfile
import subprocess

from n6lib.auth_db.models import SERVICE_CA_PROFILE_NAME
from n6lib.auth_db.validators import is_cert_serial_number_valid
from n6lib.common_helpers import read_file
from n6lib.config import ConfigString
from n6lib.const import CERTIFICATE_SERIAL_NUMBER_HEXDIGIT_NUM
from n6lib.datetime_helpers import datetime_utc_normalize
from n6lib.x509_helpers import normalize_hex_serial_number


SERVER_COMPONENT_ADDITIONAL_OPENSSL_COMMAND_ARGS = ('-policy', 'server_component_serviceCA_policy')

DEFAULT_INDEX_ATTR_CONTENT = 'unique_subject = no'

PKCS11_OPTS_PATTERN = '''
openssl_conf = openssl_def

[openssl_def]
engines = engine_section

[engine_section]
pkcs11 = pkcs11_section

[pkcs11_section]
engine_id = pkcs11
dynamic_path = {pkcs11_dynamic_path}
MODULE_PATH = {pkcs11_module_path}
init = 0
'''
# ^
# example `pkcs11_dynamic_path` value: "/usr/lib/engines/engine_pkcs11.so"
# example `pkcs11_module_path` value: "/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so"
#
# The values of these two formattable fields will be taken from the
# appropriate `ca_key_...` option (e.g., `ca_key_client_2 =
# pkcs11:/usr/lib/engines/engine_pkcs11.so:/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so:-keyfile foo:bar -keyform spam`)
# in the appropriate section of the n6 config (by default, the section
# is `[manage_api]`).


#
# Functions used by the n6lib.manage_api._manage_api stuff
#

def get_ca_env_configuration(ca, ca_key_path):
    ca_env_configuration = dict(
        ca=ca,
        tmp_env_init_kwargs_base=dict(
            ssl_conf=ca.ssl_config,
            index_attr=DEFAULT_INDEX_ATTR_CONTENT,
            ca_cert=ca.certificate,
        ),
    )
    if ca_key_path.startswith('pkcs11:'):
        (_,
         pkcs11_dynamic_path,
         pkcs11_module_path,
         pkcs11_additional_openssl_cmd_args) = ca_key_path.split(':', 3)
        ca_env_configuration['tmp_env_init_kwargs_base']['pkcs11_opts_dict'] = {
            'pkcs11_dynamic_path': pkcs11_dynamic_path,
            'pkcs11_module_path': pkcs11_module_path,
            'pkcs11_additional_openssl_cmd_arg_list': pkcs11_additional_openssl_cmd_args.split(),
        }
    else:
        ca_env_configuration['tmp_env_init_kwargs_base']['ca_key'] = read_file(ca_key_path)
    return ca_env_configuration


def generate_certificate_pem(ca_env_configuration, csr_pem, serial_number,
                             server_component_n6login=None):
    """
    Generate a certificate.

    Args:
        `ca_env_configuration`:
            The result of a get_ca_env_configuration() call.
        `csr_pem`:
            The certificate signing request (CSR) in the PEM format, as
            a string.
        `serial_number`:
            The serial number for the generated certificate, as a string
            being a hexadecimal number.
        `server_component_n6login` (None or a string; default: None):
            Must be specified (as a non-None value) if the
            certificate that is being created belongs to an n6
            public server (the certificate's `kind` is
            "server-component"); otherwise it must be None.

    Returns:
        The generated certificate in the PEM format (as a string).
    """
    serial_number = normalize_hex_serial_number(
        serial_number,
        CERTIFICATE_SERIAL_NUMBER_HEXDIGIT_NUM)
    serial_openssl = _format_openssl_serial_number(serial_number)
    tmp_env_init_kwargs = dict(
        ca_env_configuration['tmp_env_init_kwargs_base'],
        csr=csr_pem,
        serial=serial_openssl,
        index='',
    )
    if server_component_n6login is None:
        additional_openssl_command_args = ()
    else:
        assert ca_env_configuration['ca'].profile == SERVICE_CA_PROFILE_NAME
        additional_openssl_command_args = SERVER_COMPONENT_ADDITIONAL_OPENSSL_COMMAND_ARGS
    with TmpEnv(**tmp_env_init_kwargs) as tmp_env:
        cert_pem = tmp_env.execute_cert_generation(additional_openssl_command_args)
    return cert_pem


def generate_crl_pem(ca_env_configuration):
    """
    Generate a certificate revocation list (CRL) for the specified CA.

    Args:
        `ca_env_configuration`:
            The result of a get_ca_env_configuration() call (among
            others, it specifies also the concerned CA).

    Returns:
        The generated CRL in the PEM format (as a string).
    """
    ca = ca_env_configuration['ca']
    index = _make_openssl_index_file_content(ca.iter_all_certificates())
    tmp_env_init_kwargs = dict(
        ca_env_configuration['tmp_env_init_kwargs_base'],
        index=index,
        serial='',
    )
    with TmpEnv(**tmp_env_init_kwargs) as tmp_env:
        crl_pem = tmp_env.execute_crl_generation()
    return crl_pem


def revoke_certificate_and_generate_crl_pem(ca_env_configuration, cert_data):
    """
    Revoke the specified certificate and generate a certificate
    revocation list (CRL) (for the specified CA).

    Args:
        `ca_env_configuration`:
            The result of a get_ca_env_configuration() call (among
            others, it specifies also the concerned CA).
        `cert_data`:
            The certificate that is being revoked as an instance of
            a subclass of n6lib.manage_api._manage_api._CertificateBase.

    Returns:
        The generated CRL in the PEM format (as a string).
    """
    ca = ca_env_configuration['ca']
    index = _make_openssl_index_file_content(ca.iter_all_certificates())
    serial_openssl = _format_openssl_serial_number(cert_data.serial_hex)
    tmp_env_init_kwargs = dict(
        ca_env_configuration['tmp_env_init_kwargs_base'],
        index=index,
        serial=serial_openssl,
        revoke_cert=cert_data.certificate,
    )
    with TmpEnv(**tmp_env_init_kwargs) as tmp_env:
        tmp_env.execute_cert_revocation()
        crl_pem = tmp_env.execute_crl_generation()
    return crl_pem


#
# Local helper classes
#

## For historical reasons, some implementation details are strange and
## can be made more straightforward and clean [maybe TODO later]...

class InvalidSSLConfigError(Exception):

    def __init__(self, msg, original_exc):
        self.original_exc = original_exc
        super(InvalidSSLConfigError, self).__init__(msg)


class DirectoryStructure(object):

    """The directory structure for a TmpEnv's component."""

    def __init__(self, name, rel_pth, path, opts=None):
        assert isinstance(name, basestring)
        assert isinstance(rel_pth, basestring)
        assert isinstance(path, basestring)
        self.name = name
        self.relative_pth = rel_pth
        self._value = None
        self._path = path.rstrip('/') + '/'
        self.opts = (opts if opts is not None else None)
        self._makedir_if_nonexistent()

    def _makedir_if_nonexistent(self):
        dir_path = osp.dirname(self.path)
        if not osp.exists(dir_path):
            os.makedirs(dir_path)


    @property
    def path(self):
        return self._path + self.relative_pth + self.name


    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        if self.name == 'openssl.cnf':
            value = self._get_adjusted_openssl_config_str(value)
        self._value = value
        self._create_file()


    def _get_adjusted_openssl_config_str(self, value):
        try:
            value = ConfigString(value)
        except ValueError as exc:
            raise InvalidSSLConfigError("SSL config is not valid: {}.".format(exc), exc)
        ca_opt_pattern = value.get_opt_value('ca.default_ca') + '.{}'

        value = self._get_openssl_config_with_substituted_paths(ca_opt_pattern, value)

        pkcs11_opts_dict = self.opts.get('pkcs11_opts_dict')
        if pkcs11_opts_dict:
            pkcs11_opts = PKCS11_OPTS_PATTERN.format(**pkcs11_opts_dict)
            value = value.insert_above('ca', pkcs11_opts)
            value = value.remove(ca_opt_pattern.format('private_key'))

        return value


    def _get_openssl_config_with_substituted_paths(self, ca_opt_pattern, value):
        value = self._substitute_path(ca_opt_pattern, value, 'dir', osp.dirname(self.path))

        # unify temporary environment paths and config paths,
        # so they do not differ when used by OpenSSL
        paths_mapping = self.opts.get('paths_to_substitute')
        if paths_mapping:
            for opt_name, tmp_path in paths_mapping.iteritems():
                value = self._substitute_path(ca_opt_pattern, value, opt_name, tmp_path)

        return value


    def _substitute_path(self, ca_opt_pattern, value, opt_name, tmp_path):
        config_opt = ca_opt_pattern.format(opt_name)
        return value.substitute(config_opt, '{} = {}'.format(opt_name, tmp_path))


    def _create_file(self):
        if not osp.isdir(self.path):
            with open(self.path, 'w') as f:
                f.write(self.value)



class TmpEnv(object):

    """
    Temporary environment for OpenSSL CA operations.
    """

    def __init__(self, pkcs11_opts_dict=None, **init_values):
        self.pkcs11_opts_dict = pkcs11_opts_dict
        path = self.tmp_path_templ = tempfile.mkdtemp()
        try:
            self._prepare_dir_structures(path)
            for name, value in sorted(init_values.iteritems()):
                dir_struct = getattr(self, name)
                dir_struct.value = value
        except:
            self._cleanup()
            raise

    def _prepare_dir_structures(self, path):
        self.ca_cert = DirectoryStructure(name='cacert.pem', rel_pth='', path=path)
        self.ca_key = DirectoryStructure(name='cakey.pem', rel_pth='private/', path=path)
        self.certs_dir = DirectoryStructure(name='', rel_pth='certs/', path=path)
        self.csr = DirectoryStructure(name='client.csr', rel_pth='csr/', path=path)
        self.index = DirectoryStructure(name='index.txt', rel_pth='', path=path)
        self.index_attr = DirectoryStructure(name='index.txt.attr', rel_pth='', path=path)
        self.revoke_cert = DirectoryStructure(name='revoke_cert.pem', rel_pth='', path=path)
        self.ca_crl = DirectoryStructure(name='ca.crl', rel_pth='', path=path)
        self.serial = DirectoryStructure(name='serial', rel_pth='', path=path)
        self.gen_cert = DirectoryStructure(name='cert.pem', rel_pth=self.certs_dir.relative_pth,
                                           path=path)
        paths_to_substitute = self._get_paths_to_substitute_dict()
        self.ssl_conf = DirectoryStructure(name='openssl.cnf', rel_pth='', path=path,
                                           opts={'pkcs11_opts_dict': self.pkcs11_opts_dict,
                                                 'paths_to_substitute': paths_to_substitute})

    def _get_paths_to_substitute_dict(self):
        return {
            'certificate': self.ca_cert.path,
            'private_key': self.ca_key.path,
            'new_certs_dir': self.certs_dir.path,
            'database': self.index.path,
            'serial': self.serial.path,
        }

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._cleanup()

    def _cleanup(self):
        shutil.rmtree(self.tmp_path_templ)

    def execute_cert_generation(self, additional_openssl_command_args):
        self._execute_command(
            [
                'openssl', 'ca',
                '-config', self.ssl_conf.path,
                '-notext',
                '-in', self.csr.path,
                '-out', self.gen_cert.path,
                '-batch',
            ]
            + self._get_pkcs11_openssl_command_args()
            + list(additional_openssl_command_args))
        return read_file(self.gen_cert.path)

    def execute_cert_revocation(self):
        self._execute_command(
            [
                'openssl', 'ca',
                '-config', self.ssl_conf.path,
                '-revoke', self.revoke_cert.path,
                '-batch',
            ] + self._get_pkcs11_openssl_command_args())

    def execute_crl_generation(self):
        self._execute_command(
            [
                'openssl', 'ca',
                '-config', self.ssl_conf.path,
                '-gencrl',
                '-out', self.ca_crl.path,
                '-batch',
            ] + self._get_pkcs11_openssl_command_args())
        return read_file(self.ca_crl.path)

    @staticmethod
    def _execute_command(cmd_args):
        try:
            subprocess.check_output(cmd_args, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError('CA env error ({0}; {1!r})'.format(exc, exc.output))

    def _get_pkcs11_openssl_command_args(self):
        openssl_command_args = []
        if self.pkcs11_opts_dict is not None:
            ca_sect_name = self.ssl_conf.value.get_opt_value('ca.default_ca')
            openssl_command_args.extend([
                '-engine', 'pkcs11',
            ] + self.pkcs11_opts_dict['pkcs11_additional_openssl_cmd_arg_list'] + [
                '-name', ca_sect_name,
            ])
        return openssl_command_args


#
# Local helper functions
#

def _make_openssl_index_file_content(cert_data_iterator):
    # [The following comment was copied from ???]
    #
    # The index.txt file is an ascii file consisting of 6 tab-separated
    # fields.  Some of those fields may be empty and might appear not to exist at all.
    #
    # The 6 fields are:
    #
    # 0)  Entry type.  May be "V" (valid), "R" (revoked) or "E" (expired).
    #     Note that an expired may have the type "V" because the type has
    #     not been updated.  'openssl ca updatedb' does such an update.
    # 1)  Expiration datetime.
    #     The format of the date is YYMMDDHHMMSSZ
    #      (the same as an ASN1 UTCTime structure)
    # 2)  Revokation datetime.  This is set for any entry of the type "R".
    #     The format of the date is YYMMDDHHMMSSZ
    #      (the same as an ASN1 UTCTime structure)
    # 3)  Serial number.
    # 4)  File name of the certificate.  This doesn't seem to be used,
    #     ever, so it's always "unknown".
    # 5)  Certificate subject name.
    #
    # So the format is:
    #     E|R|V<tab>Expiry<tab>[RevocationDate]<tab>Serial<tab>unknown<tab>SubjectDN

    index_file_lines = []

    for cert_data in cert_data_iterator:
        entry_type = 'V'
        expires_openssl = revoked_openssl = ''

        if cert_data.expires_on is not None:
            if datetime.datetime.utcnow() > cert_data.expires_on:
                entry_type = 'E'
            expires_openssl = _format_openssl_dt(cert_data.expires_on)

        if cert_data.revoked_on is not None:
            entry_type = 'R'
            revoked_openssl = _format_openssl_dt(cert_data.revoked_on)

        serial_openssl = _format_openssl_serial_number(cert_data.serial_hex)
        subject = cert_data.subject

        index_file_lines.append("{0}\t{1}\t{2}\t{3}\tunknown\t{4}\n".format(
            entry_type,
            expires_openssl,
            revoked_openssl,
            serial_openssl,
            subject))

    return ''.join(index_file_lines)


def _format_openssl_dt(dt):
    """The format of the date is YYMMDDHHMMSSZ (the same as an ASN1 UTCTime structure)

           Arg: dt <datetime>

           Ret: <string> (format YYMMDDHHMMSSZ)

           Raises: AssertionError (if format is not datetime)

    >>> naive_dt_1 = datetime.datetime(2013, 6, 6, 12, 13, 57)
    >>> _format_openssl_dt(naive_dt_1)
    '130606121357Z'

    >>> naive_dt_2 = datetime.datetime(2013, 6, 6, 12, 13, 57, 951211)
    >>> _format_openssl_dt(naive_dt_2)
    '130606121357Z'

    >>> from n6lib.datetime_helpers import FixedOffsetTimezone
    >>> tz_aware_dt = datetime.datetime(
    ...     2013, 6, 6, 14, 13, 57, 951211,   # note: 14 instead of 12
    ...     tzinfo=FixedOffsetTimezone(120))
    >>> _format_openssl_dt(tz_aware_dt)
    '130606121357Z'

    >>> _format_openssl_dt('2014-08-08 12:01:23')  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
        ...
    TypeError: a datetime.datetime instance is required
    >>> _format_openssl_dt(None)                   # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
        ...
    TypeError: a datetime.datetime instance is required
    """
    if isinstance(dt, datetime.datetime):
        return datetime_utc_normalize(dt).strftime("%y%m%d%H%M%SZ")
    raise TypeError('a datetime.datetime instance is required')


def _format_openssl_serial_number(serial_number):
    if isinstance(serial_number, unicode):
        serial_number = str(serial_number)
    if not isinstance(serial_number, str):
        raise TypeError('serial_number {!r} has a wrong type ({})'.format(
            serial_number,
            type(serial_number).__name__))
    serial_number = serial_number.upper()
    if len(serial_number) % 2:
        # force even number of digits
        serial_number = '0' + serial_number
    # sanity check
    if not is_cert_serial_number_valid(serial_number.lower()):
        raise ValueError(
            'something really wrong: a certificate serial number '
            'prepared for OpenSSL tools ({0!r}) is not valid'
            .format(serial_number))
    return serial_number


if __name__ == "__main__":
    import doctest
    doctest.testmod()

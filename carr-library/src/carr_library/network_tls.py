"""Verified client TLS without environment-triggered session-secret logging."""
import ssl


def client_context():
    # create_default_context() consults SSLKEYLOGFILE and creates that file
    # before a connection begins. Use the documented client context directly;
    # do not temporarily change the process environment shared by other workers.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    return context

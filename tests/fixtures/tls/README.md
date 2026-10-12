# TLS test credentials

`controller.crt` and `controller.key` are a self-signed certificate and its
publicly known private key for automated tests. The certificate is valid for
100 years (36,525 days) and identifies `rmote.test`.

**Never use this certificate or private key in production. Never add this
certificate to a system or production trust store.** Anyone can impersonate
its owner because the private key is included in this repository.

The tests trust this certificate only in their own TLS contexts. The remote
test process receives the public certificate; the private key stays on the
controller side.

`untrusted.crt` and `untrusted.key` are a second, independent test pair with
the same lifetime and restrictions. They verify rejection of a controller
whose certificate is not trusted. Generate them with the command below,
replacing `controller` with `untrusted` and `rmote.test` with
`untrusted.rmote.test`.

To generate a replacement with OpenSSL:

```sh
openssl req -x509 -newkey rsa:2048 -sha256 -nodes \
  -keyout controller.key -out controller.crt -days 36525 \
  -subj '/CN=rmote.test' \
  -addext 'subjectAltName=DNS:rmote.test' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign' \
  -addext 'extendedKeyUsage=serverAuth'
```

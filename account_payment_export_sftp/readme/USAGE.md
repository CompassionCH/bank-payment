To use this module, you need to:

1.  Go to EDI and configure a backend and an exchange type
2.  Create a payment order and choose an EDI exchange type
3.  When generating the file an edi exchange should be created

For an SFTP FS storage with an SSH key, put the private key as text in the
`pkey` option. Example: `{"host": "sftp.example.com", "port": 22,
"username": "user", "pkey": "-----BEGIN RSA PRIVATE KEY-----\n..."}`.

## 18.0.1.0.1 (2026-10-15)

- \[FIX\] Migration: create the SFTP FS storage from the old storage
  backend and link the EDI backends and exchange records to it. The
  16.0 migration of edi_storage_oca did not do it.
- \[ADD\] An SFTP FS storage can keep its private key as text in the
  `pkey` option.

## 14.0.1.0.0 (2023-06-21)

- \[ADD\] Account_payment_export_sftp

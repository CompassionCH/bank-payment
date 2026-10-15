-- A copy of the database must not send files to the bank SFTP server:
-- without a storage, the EDI backends cannot send or read any file.
UPDATE edi_backend
   SET storage_id = NULL
 WHERE storage_id IS NOT NULL;

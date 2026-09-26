"""Host-side updater components; never imported to grant web-app privileges.

The protocol and transaction journal are independent of the application database.
Only an explicitly provisioned sealed host daemon executes privileged actions;
importing these modules into the Web app never grants host privilege.
"""

# Kestrel Ridge Logistics: IT policy handbook

Version 1.0, effective 2026-11-01. Owner: IT. This handbook is fictional and written for the helpdesk-copilot demo; Kestrel Ridge Logistics is not a real company.

Each section has an id in brackets, such as `[pw-1]`. Answers from the copilot cite these ids.

## Passwords and sign-in

### [pw-1] Password rules

Passwords must be at least 14 characters long. They expire every 90 days, and the last 10 passwords cannot be reused. Change your password yourself at the self-service portal (portal.kestrelridge.example) before it expires; the portal reminds you 14 days ahead. An expired password stops sign-in to email, Wi-Fi and VPN until it is changed.

### [pw-2] Account lockout

After 5 failed sign-in attempts the account locks. Only the IT helpdesk can unlock it, and only after verifying your identity (see [pw-4]). An unlock is an account change, so the helpdesk agent must approve it. If you did not cause the failed attempts yourself, say so: it may be someone else trying your password, and the security team is told.

### [pw-3] Multi-factor authentication

MFA is required for every account. Use the authenticator app; finance staff and IT administrators use a hardware key. SMS codes are allowed only as a temporary method, for at most 30 days. If you get a new phone or your MFA locks after too many wrong codes, the helpdesk resets MFA after verifying your identity, and you enrol again. An MFA reset is an account change and needs the agent's approval. If sign-in fails for everyone because single sign-on is down, MFA is not reset: wait for the service to recover.

### [pw-4] Identity verification

Before any account change (unlock, MFA reset, password reset), the helpdesk verifies who you are by calling you back on the phone number in the directory, or by written confirmation from your manager. Verification never happens by replying to an email or chat message alone.

## VPN and remote work

### [vpn-1] When to use the VPN

Use the VPN for internal systems whenever you are outside a Kestrel Ridge office. On the office Wi-Fi or a wired office connection you do not need it.

### [vpn-2] VPN certificates

Each VPN client has a personal certificate valid for 12 months. The VPN client warns you 30 days before it expires. An expired certificate stops the VPN from connecting; the helpdesk issues a new one, which does not need the agent's approval because it does not change your account.

### [vpn-3] VPN client version

Only VPN client versions offered in the software catalog are supported, and the gateway rejects versions older than 5.2. Clients update themselves; if yours is too old, update it from the catalog.

### [vpn-4] Public Wi-Fi

Public Wi-Fi (hotels, cafes, airports) is allowed only with the VPN connected. Do not print or share your screen in public places.

## Software

### [sw-1] The software catalog

Install software only from the software catalog. Staff do not have administrator rights. If the catalog is unavailable, check the status page and try again later.

### [sw-2] Requesting new software

Software not in the catalog is requested with the software request form. It needs your manager's approval and a security review, which takes up to 5 business days.

### [sw-3] Updates and compliance

Security updates must be installed within 7 days of release. A device with updates pending for more than 14 days, or with antivirus definitions more than 7 days old, is marked non-compliant. Non-compliant devices are blocked from the office network and from installing software until they are brought up to date.

### [sw-4] Prohibited software

Games, personal cloud storage, remote-control tools and browser extensions not in the catalog are prohibited on company devices.

## Devices

### [dev-1] Device compliance

Every device must have disk encryption on, current antivirus definitions and current updates (see [sw-3]). Compliance is checked automatically every day.

### [dev-2] Storage

Keep at least 15% of the disk free. Save work files to company cloud storage, not the desktop or downloads folder. If the disk is nearly full, clear downloads first; ask the helpdesk before deleting anything you are unsure about.

### [dev-3] Overheating and hardware care

Use laptops on a hard, flat surface and keep the vents clear. A laptop that runs hot during light work needs attention: install pending updates first, then book hardware service if it stays hot. Hardware repairs are done only by IT; a loaner laptop is available within 1 business day.

### [dev-4] Lost or stolen devices

Report a lost or stolen device to the helpdesk within 1 hour. IT locks and remotely wipes it.

## Email and phishing

### [mail-1] Mailbox size

Each mailbox holds 50 GB. When it is full you cannot send email. Archive or delete old mail, starting with large attachments.

### [mail-2] Reporting phishing

Report suspicious email with the Report button; do not forward it. If you clicked a link or entered your password, contact the helpdesk at once and change your password at the self-service portal. The security team reviews every report.

### [mail-3] Forwarding and sharing

Automatic forwarding of company email to personal accounts is not allowed. Share files from company cloud storage instead of attaching them.

### [mail-4] Service outages

Known outages are posted on the status page (status.kestrelridge.example). You do not need to open a ticket for a known outage; the helpdesk links the status page and updates you when it is resolved.

## Printing

### [prn-1] Printers and locations

The head office has two printers (PRN-HQ-1 and PRN-HQ-2) and the warehouse has one (PRN-WH-1). Remote staff cannot print to office printers.

### [prn-2] Confidential printing

Print confidential documents with secure release: the job waits until you badge in at the printer. Collect it within 10 minutes. Unreleased jobs are deleted after 24 hours.

### [prn-3] Printer problems

Clear paper jams by following the steps on the printer's panel. If a printer shows as offline, report it to the helpdesk; do not unplug or move printers yourself.

# Validation report: `draft-2026-11-28`

Draft built by `python -m app.knowledge.resolve` from the extraction of 2026-11-28 (`evals/results/extraction.json`). No model calls; the same extraction always gives this report.

**Valid.** 13 issues; 96 symptoms, 135 possible causes, 181 solutions, 101 diagnostics, of which 19 map to a tool. For review: **1 conflict**, 1 possible duplicate, 82 diagnostics with no tool. 0 groups merged; 4 items left out for missing evidence.

Diagnostics and solutions are not linked to causes yet: extraction does not say which check tests which cause, so the reviewer links them.

## By issue

| Issue | Symptoms | Causes | Diagnostics (with a tool) | Solutions |
| --- | ---: | ---: | ---: | ---: |
| `network--vpn-not-connecting` | 12 | 13 | 12 (3) | 13 |
| `network--wi-fi-not-connecting` | 8 | 12 | 8 (0) | 12 |
| `microsoft-office--outlook-not-sending-emails` | 15 | 18 | 14 (3) | 23 |
| `system--disk-space-running-out` | 6 | 15 | 8 (2) | 22 |
| `system--system-running-slow` | 6 | 16 | 9 (2) | 22 |
| `hardware--laptop-overheating` | 3 | 6 | 5 (0) | 4 |
| `software--program-won-t-install` | 7 | 6 | 8 (1) | 15 |
| `software--application-not-updating` | 6 | 8 | 6 (1) | 17 |
| `security--antivirus-not-updating` | 13 | 10 | 12 (1) | 13 |
| `security--phishing-email-detected` | 11 | 5 | 4 (0) | 12 |
| `hardware--printer-not-responding` | 6 | 21 | 13 (5) | 20 |
| `identity--account-locked` | 1 | 2 | 1 (0) | 4 |
| `identity--mfa-lockout` | 2 | 3 | 1 (1) | 4 |

## Conflicts: sources disagree, both kept

| Issue | Kind | One source says | Another says |
| --- | --- | --- | --- |
| `microsoft-office--outlook-not-sending-emails` | diagnostic | Check if stuck messages in the Outbox have attachments larger than 20MB. (support-microsoft-com-en-us-outlook-i-can-t-send-or-receive-messages-in-outlook) | Check whether the message attachment is larger than 25MB. (support-microsoft-com-en-us-outlook-outlook-email-stuck) |

## Possible duplicates: similar wording from different sources, not merged

| Issue | Kind | A | B | Similarity |
| --- | --- | --- | --- | ---: |
| `security--antivirus-not-updating` | cause | Required URLs for security intelligence updates are blocked by a firewall or proxy. | The firewall or proxy is blocking the URLs required for Security intelligence updates. | 0.78 |

## Diagnostics with no tool

Kept out of the release: the copilot cannot run them on the company's systems. A reviewer can map one to a tool or leave it as advice for the agent.

- `network--vpn-not-connecting`: Run a whatismyip scan to check if the template machine has a public IP address.; Check the VPN Profile properties General tab to verify the value can publicly resolve through DNS.; Open ICMP to the external interface and ping the VPN server from the remote client.; Check the configuration of the internal and external NICs on the VPN server.; Check the client, server, and hardware firewalls to verify they allow UDP 500 and 4500 port activity.; Verify the NPS server has a Server Authentication certificate that can service IKE requests.; Check the NPS event log for authentication issues.; Check the NPS logs to diagnose policy-related issues.; Run the Get-WmiObject cmdlet to dump the VPN profile configuration.
- `network--wi-fi-not-connecting`: Check if your network name shows Connected below it on the taskbar.; Try connecting to the same network on a different device to determine if your device is the problem.; Use a ping test to check if there is a problem with the connection to your Wi-Fi router.; Connect your PC directly to the modem using an Ethernet cable to confirm if the Wi-Fi router is the problem.; Check your IP address to see if it starts with 169.254.x.x.; Verify that you have a DNS server listed and that you can ping it.; Check the wireless network report for a Problem number to find out why the driver did not load.; Check the Driver Version and Driver Date to see if they match the latest versions on the manufacturer's website.
- `microsoft-office--outlook-not-sending-emails`: Open a web browser and try visiting Microsoft or Bing websites to check internet connectivity.; Force a manual sync using the Sync button on the View tab to check for sync issues.; Check if stuck messages in the Outbox have attachments larger than 20MB.; Check the bottom of the Outlook window for Disconnected, Working Offline, or Trying to connect status.; Start Outlook in safe mode to determine if an add-in is causing the problem.; Check whether the Work Offline button is blue, which indicates disconnection.; Check whether the Send immediately when connected option is enabled in Outlook settings.; Check whether the Send immediately when connected setting is grayed out, indicating group policy control.; Start Outlook in Safe mode to see if the issue persists.; Check whether the stuck email is in the Drafts or Outbox folder.; Check whether the message attachment is larger than 25MB.
- `system--disk-space-running-out`: Click the More Info button next to a listed category to see details and management options for that category.; Check the Users folder on Macintosh HD for undeleted data from deleted users and unneeded shared files.; Identify which files are using the most space by browsing folders in File Explorer.; Check the Videos, Music, Pictures, and Downloads folders as they usually contain the largest files.; Sort files by size to show the largest files at the top of the list.; Use Cleanup recommendations to identify temporary files and system files that can be removed.
- `system--system-running-slow`: Use the Microsoft PC Manager app to help diagnose issues.; Run a malware scan to look for threats.; Monitor CPU, memory, and disk usage to find heavy processes.; Review the Performance and Startup apps tabs to find components using high resources.; Identify which files are using the most space by browsing folders in File Explorer.; Sort files by size in a folder to show the largest files at the top of the list.; Review Cleanup recommendations categories including Temporary files, Large or unused files, Files synced to the cloud, and Unused apps.
- `hardware--laptop-overheating`: Check if the processor is working on intensive tasks such as compressing HD video or playing graphics-heavy games.; Check the ambient temperature outside the device.; Check whether the vents are blocked.; Check what surface the device is being used on.; Verify if fans run fast even when the device isn't experiencing heavy usage and is properly ventilated.
- `software--program-won-t-install`: Verify that the app is compatible with the Windows version.; Check for available updates in Microsoft Store.; Run the Windows Store apps troubleshooter to identify issues.; Check whether the app was installed from Microsoft Store to determine the correct troubleshooting path.; Run Windows Security to scan for malware or suspicious apps.; Check whether the program appears in the uninstall options list.; Follow any guidance provided in error messages or notifications from Windows.
- `software--application-not-updating`: Verify that the app is compatible with your version of Windows.; Check for available updates to Microsoft Store itself.; Run the Windows Store apps troubleshooter to identify problems.; Windows 11 may automatically recommend troubleshooting steps when installation or removal fails.; The troubleshooter asks for the program's product code if the program is not listed in the uninstall options.
- `security--antivirus-not-updating`: Check the Defender for Endpoint URL spreadsheets to verify connectivity to required URLs.; Review the event log for error code 0x80072ee7 to determine if SSL inspection is causing the issue.; Verify that the Windows Update service and BITS are both started.; Check the Fallback order policy to confirm Microsoft Update is listed first.; Gather diagnostic data using the Microsoft Defender for Endpoint Client Analyzer tool.; Convert WindowsUpdate.etl logs into a readable format using the Get-WindowsUpdateLog PowerShell command.; Check the URLs required for Security intelligence updates via the firewall and/or proxy.; Manually update Security Intelligence by downloading and installing updates from the Microsoft website.; Try updating through the Microsoft Malware Protection Center using the MpCmdRun command.; Review the WUServer registry key to check if a WSUS server is configured.; Verify that the WSUS server has the MDAV security intelligence updates approved.
- `security--phishing-email-detected`: Verify if the displayed URL matches the actual destination URL.; Contact the business by known channels to verify if suspicious emails are legitimate.; Check if the email is from an entity or person you rarely deal with.; Check if the sender address matches the signature on the message.
- `hardware--printer-not-responding`: Run the automated printer troubleshooter in the Get Help app to diagnose problems.; Verify that the printer's USB cable is properly connected.; Look for a blue wireless icon on the printer to check if the wireless option is available.; Run the printer's wireless connectivity test from its menu.; Inspect the printer for low paper or ink, an open cover, or a paper jam.; Print a test page to verify the printer is working correctly.; Check the printer's built-in menu or manual to verify its Wi-Fi connection status.; Check whether the Set as default option is missing, which indicates Let Windows manage my default printer is enabled.
- `identity--account-locked`: Ask the user whether they were the one who made the failed sign-in attempts.

## Diagnostics mapped to a tool

| Issue | Diagnostic | Tool |
| --- | --- | --- |
| `network--vpn-not-connecting` | Look for events labeled RasClient in the application logs on client machines. | `vpn_last_session` |
| `network--vpn-not-connecting` | Use Event Viewer to find error 812 marked as event log 20276. | `vpn_last_session` |
| `network--vpn-not-connecting` | Run the Certutil command to check for valid certificates in the user's certificate store. | `vpn_certificate_status` |
| `microsoft-office--outlook-not-sending-emails` | Check whether the email account password was recently changed on the provider's website. | `account_status` |
| `microsoft-office--outlook-not-sending-emails` | Go to mailbox storage to check what is taking up space. | `account_status` |
| `microsoft-office--outlook-not-sending-emails` | Check whether the Inbox is full. | `account_status` |
| `system--disk-space-running-out` | Check the Storage pane in System Settings or About This Mac to see how much space is used by various file categories. | `device_health` |
| `system--disk-space-running-out` | Check the total disk space left by opening File Explorer and selecting This PC. | `device_health` |
| `system--system-running-slow` | Check for available Windows updates. | `device_health` |
| `system--system-running-slow` | Check the total disk space left by opening File Explorer and selecting This PC to view available space under Devices and drives. | `device_health` |
| `software--program-won-t-install` | Check for Windows updates to see if the system is current. | `device_health` |
| `software--application-not-updating` | Check whether Windows has any available updates pending installation. | `device_health` |
| `security--antivirus-not-updating` | Verify that Microsoft Defender Antivirus is set as the primary antivirus. | `installed_software` |
| `hardware--printer-not-responding` | Check if the printer's status displays an error state. | `printer_status` |
| `hardware--printer-not-responding` | Check if the printer displays an offline status. | `printer_status` |
| `hardware--printer-not-responding` | Check whether the printer shows as Offline despite being powered on and connected. | `printer_status` |
| `hardware--printer-not-responding` | Open the printer queue to check for any pending print jobs. | `printer_status` |
| `hardware--printer-not-responding` | Check whether Pause Printing or Use Printer Offline is selected in the printer queue settings on Windows 10. | `printer_status` |
| `identity--mfa-lockout` | Determine whether sign-in fails for everyone or just one user to distinguish an SSO outage from an individual MFA lockout. | `service_status` |

## Left out: evidence not found in the source

- `system--disk-space-running-out` solution: Reset Microsoft Store to fix the Temp folder filling with .appx files. (support-microsoft-com-en-us-windows-free-up-drive-space-in-windows-85529ccb-c365-490d-b548-831022bc9)
- `system--disk-space-running-out` solution: Run the Windows Update troubleshooter to reset Windows Update. (support-microsoft-com-en-us-windows-free-up-drive-space-in-windows-85529ccb-c365-490d-b548-831022bc9)
- `system--system-running-slow` solution: Reset Microsoft Store by running the Windows Store Apps troubleshooter. (support-microsoft-com-en-us-windows-free-up-drive-space-in-windows-85529ccb-c365-490d-b548-831022bc9)
- `system--system-running-slow` solution: Reset Windows Update by running the Windows Update troubleshooter. (support-microsoft-com-en-us-windows-free-up-drive-space-in-windows-85529ccb-c365-490d-b548-831022bc9)

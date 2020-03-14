import os
import smtplib
import subprocess

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

# Email you want to send the update from (only works with gmail)
fromEmail = 'jon.hesthus@gmail.com'
# You can generate an app password here to avoid storing your password in plain text
# https://support.google.com/accounts/answer/185833?hl=en
fromEmailPassword = os.environ['GMAIL_PASS']

# Email you want to send the update to
toEmail = 'jon.hesthus@gmail.com'


def sendEmail(image):
	msgRoot = MIMEMultipart('related')
	msgRoot['Subject'] = 'Latest IP info'
	msgRoot['From'] = fromEmail
	msgRoot['To'] = toEmail
	msgRoot.preamble = 'ip address info'

	msgAlternative = MIMEMultipart('alternative')
	msgRoot.attach(msgAlternative)
    ifconfig = subprocess.check_output('ifconfig', shell=True)

	msgText = MIMEText(ifconfig)
	msgAlternative.attach(msgText)

	smtp = smtplib.SMTP('smtp.gmail.com', 587)
	smtp.starttls()
	smtp.login(fromEmail, fromEmailPassword)
	smtp.sendmail(fromEmail, toEmail, msgRoot.as_string())
	smtp.quit()

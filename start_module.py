import sys
import subprocess

installed_dependencies = subprocess.check_output(
    [sys.executable, '-m', 'pip', 'install', '-r', 'python_dependencies.ini']).decode().strip()
if 'Successfully installed' in installed_dependencies:
    raise Exception('Some required dependent libraries were installed. ' \
        'Module execution has to be terminated now to use installed libraries on the next scheduled launch.')

import json
import re
# from jsonschema import validate
from onevizion import IntegrationLog, LogLevel


with open('settings.json', 'rb') as settings_file:
    settings_data = json.loads(settings_file.read().decode('utf-8'))

# ====== Run main.py after completion
subprocess.run(
    [sys.executable, "main.py"],
    check=True
)
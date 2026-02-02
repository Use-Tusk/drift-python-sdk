#!/usr/bin/env python3
"""Test app for SDK benchmarks. Respects TUSK_DRIFT_MODE env var."""
import logging
import os
import requests as req
from flask import Flask, request, jsonify

# Disable Flask/Werkzeug request logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# SDK initialization based on env var
MODE = os.environ.get('TUSK_DRIFT_MODE', 'DISABLED')
if MODE != 'DISABLED':
    from drift import TuskDrift
    TuskDrift.initialize(
        api_key="benchmark-key",
        env="benchmark",
        log_level="error",
    )

app = Flask(__name__)
DELAY_SERVER = os.environ.get('DELAY_SERVER', 'http://127.0.0.1:9999')

@app.route('/health')
def health():
    return 'ok'

@app.route('/api/sort', methods=['POST'])
def api_sort():
    data = request.json['data']
    return jsonify({'sorted': sorted(data)})

@app.route('/api/downstream', methods=['POST'])
def api_downstream():
    delay_ms = request.json.get('delay_ms', 10)
    req.get(f'{DELAY_SERVER}/delay?ms={delay_ms}')
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='127.0.0.1', port=port, threaded=True)

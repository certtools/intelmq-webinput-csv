# -*- coding: utf-8 -*-
# Copyright (c) 2017-2018 nic.at GmbH <wagner@cert.at>
# SPDX-License-Identifier: AGPL-3.0
import json
import pkg_resources
import traceback
import logging
import os

from flask import Flask, make_response, request

from intelmq import HARMONIZATION_CONF_FILE
from intelmq.lib.harmonization import DateTime, IPAddress
from intelmq.bots.experts.taxonomy.expert import TAXONOMY
from intelmq.lib.message import MessageFactory
from intelmq.lib.pipeline import PipelineFactory
from intelmq.lib.utils import load_configuration

from intelmq_webinput_csv.version import __version__
from intelmq_webinput_csv.lib.exceptions import InvalidCSVLineException

from lib import util
from lib.csv import CSV

HARMONIZATION_CONF_FILE = '/config/configs/webinput/harmonization.conf'

CONFIG_FILE = os.path.join('/config/configs/webinput', 'webinput_csv.conf')
logging.info('Reading configuration from %r.', CONFIG_FILE)
with open(CONFIG_FILE) as handle:
    CONFIG = json.load(handle)
    BASE_URL = CONFIG.get('base_url', '')
    if BASE_URL.endswith('/'):
        BASE_URL = BASE_URL[:-1]

CUSTOM_FIELDS_HTML_TEMPLATE = """
<div class="field">
    <div class="control">
        <label class="label">{name}</label>
        <input class="input" type="text" placeholder="{name}" v-model="previewFormData.{jsname}">
    </div>
</div>"""
CUSTOM_FIELDS_JS_DEFAULT_TEMPLATE = "{jsname}: '{default}',"
CUSTOM_FIELDS_JS_FORM_TEMPLATE = "formData.append('custom_{name}', this.previewFormData.{jsname});"
custom_fields_html = []
custom_fields_js_default = []
custom_fields_js_form = []
for key, value in CONFIG.get('custom_input_fields', {}).items():
    jskey = 'custom' + key.title().replace('.', '')
    custom_fields_html.append(CUSTOM_FIELDS_HTML_TEMPLATE.format(name=key, jsname=jskey))
    custom_fields_js_default.append(CUSTOM_FIELDS_JS_DEFAULT_TEMPLATE.format(jsname=jskey, default=value))
    custom_fields_js_form.append(CUSTOM_FIELDS_JS_FORM_TEMPLATE.format(name=key, jsname=jskey))

STATIC_FILES = {
    'js/preview.js': None,
    'js/upload.js': None,
    'preview.html': None,
    'index.html': None,
}

for static_file in STATIC_FILES.keys():
    filename = pkg_resources.resource_filename('intelmq_webinput_csv', 'static/%s' % static_file)
    with open(filename, encoding='utf8') as handle:
        STATIC_FILES[static_file] = handle.read()
        if static_file.startswith('js/') or static_file.endswith('.html'):
            STATIC_FILES[static_file] = STATIC_FILES[static_file].replace('__BASE_URL__', BASE_URL)
            STATIC_FILES[static_file] = STATIC_FILES[static_file].replace('__VERSION__', __version__)
        if static_file == 'preview.html':
            STATIC_FILES[static_file] = STATIC_FILES[static_file].replace('__CUSTOM_FIELDS_HTML__',
                                                                          '\n'.join(custom_fields_html))
        if static_file == 'js/preview.js':
            STATIC_FILES[static_file] = STATIC_FILES[static_file].replace('__CUSTOM_FIELDS_JS_DEFAULT__',
                                                                          '\n'.join(custom_fields_js_default))
            STATIC_FILES[static_file] = STATIC_FILES[static_file].replace('__CUSTOM_FIELDS_JS_FORM__',
                                                                          '\n'.join(custom_fields_js_form))


app = Flask('intelmq_webinput_csv')


with open(HARMONIZATION_CONF_FILE) as handle:
    EVENT_FIELDS = json.load(handle)


@app.route('/')
def form():
    response = make_response(STATIC_FILES['index.html'])
    response.mimetype = 'text/html'
    response.headers['Content-Type'] = "text/html; charset=utf-8"
    return response


@app.route('/plugins/<path:page>')
def plugins(page):
    filename = pkg_resources.resource_filename('intelmq_webinput_csv', 'static/plugins/%s' % page)
    with open(filename, mode='rb') as handle:
        response = make_response(handle.read())
    if page.endswith('.js'):
        response.mimetype = 'application/x-javascript'
        response.headers['Content-Type'] = "application/x-javascript; charset=utf-8"
    elif page.endswith('.css'):
        response.mimetype = 'text/css'
        response.headers['Content-Type'] = "text/css; charset=utf-8"
    return response


@app.route('/js/<page>')
def js(page):
    response = make_response(STATIC_FILES['js/%s' % page])
    response.mimetype = 'application/x-javascript'
    response.headers['Content-Type'] = "application/x-javascript; charset=utf-8"
    return response


@app.route('/upload', methods=['POST'])
def upload_file():
    success = False
    filename = os.path.join('/config/configs/webinput', 'webinput_csv.csv')
    if 'file' in request.files and request.files['file'].filename:
        request.files['file'].save(filename)
        request.files['file'].stream.seek(0)
        total_lines = request.files['file'].stream.read().count(b'\n')  # we don't care about headers here
        success = True
    elif 'text' in request.form and request.form['text']:
        with open(filename, mode='w', encoding='utf8') as handle:
            handle.write(request.form['text'])
        success = True
        total_lines = len(request.form['text'].splitlines())
    if not success and request.form.get('use_last_file', False):
        success = True
        filename, total_lines = util.get_temp_file()
    elif success:
        util.write_temp_file((filename, total_lines))
    if not success:
        return util.create_response('no file or text')

    parameters = util.handle_parameters(request.form)
    if parameters['has_header']:
        total_lines -= 1
    preview = []
    valid_ip_addresses = None
    valid_date_times = None
    lineindex = line = None

    # Ensure Harmonization config is only loaded once
    harmonization = load_configuration(HARMONIZATION_CONF_FILE)
    try:
        with CSV.create(file=filename, harmonization=harmonization, **parameters) as reader:
            for lineindex, line in reader:

                if valid_ip_addresses is None:  # first data line
                    valid_ip_addresses = [0] * len(line)
                    valid_date_times = [0] * len(line)
                for columnindex, value in enumerate(line):
                    if IPAddress.is_valid(value):
                        valid_ip_addresses[columnindex] += 1
                    if DateTime.is_valid(value):
                        valid_date_times[columnindex] += 1
                preview.append(line.cells)
    except Exception:
        preview = [['Parse Error'], ['Is the number of columns consistent?']] + \
            [[x] for x in traceback.format_exc().splitlines()] + \
            [['Current line (%d):' % lineindex]] + \
            [line]
    column_types = ["IPAddress" if x / (total_lines if total_lines else 1) > 0.7 else None for x in valid_ip_addresses]
    column_types = ["DateTime" if valid_date_times[i] / (total_lines if total_lines else 1) > 0.7 else x for i, x in enumerate(column_types)]
    return util.create_response({"column_types": column_types,
                                 "use_column": [bool(x) for x in column_types],
                                 "preview": preview,
                                })


@app.route('/preview', methods=['GET', 'POST'])
def preview():
    if request.method == 'GET':
        response = make_response(STATIC_FILES['preview.html'])
        response.mimetype = 'text/html'
        response.headers['Content-Type'] = "text/html; charset=utf-8"
        return response

    parameters = util.handle_parameters(request.form)
    tmp_file = util.get_temp_file()
    if not tmp_file:
        app.logger.info('no file')
        return util.create_response('No file')
    exceptions = []

    # Ensure Harmonization config is only loaded once
    harmonization = load_configuration(HARMONIZATION_CONF_FILE)

    with CSV.create(file=tmp_file, harmonization=harmonization, **parameters) as reader:
        for lineindex, line in reader:

            try:
                event = line.validate()

                if CONFIG.get('destination_pipeline_queue_formatted', False):
                    CONFIG['destination_pipeline_queue'].format(ev=event)

            except InvalidCSVLineException as icle:
                exceptions.append((
                    icle.line_index,
                    icle.column_index,
                    icle.key,
                    repr(icle)
                ))

            except Exception as exc:
                exceptions.append((
                    lineindex,
                    -1,
                    CONFIG['destination_pipeline_queue'],
                    repr(exc)
                ))

        retval = {
            "total": len(reader),
            "lines_invalid": len(exceptions),
            "errors": exceptions
        }
    return util.create_response(retval)


@app.route('/classification/types')
def classification_types():
    return util.create_response(TAXONOMY)


@app.route('/harmonization/event/fields')
def harmonization_event_fields():
    return util.create_response(EVENT_FIELDS['event'])


@app.route('/submit', methods=['POST'])
def submit():
    parameters = util.handle_parameters(request.form)
    tmp_file = util.get_temp_file()
    if not tmp_file:
        return util.create_response('No file')

    destination_pipeline = PipelineFactory.create(pipeline_args=CONFIG['intelmq'],
                                                  logger=app.logger,
                                                  direction='destination')
    if not CONFIG.get('destination_pipeline_queue_formatted', False):
        destination_pipeline.set_queues(CONFIG['destination_pipeline_queue'], "destination")
        destination_pipeline.connect()

    successful_lines = 0
    parameters['time_observation'] = DateTime().generate_datetime_now()

    # Ensure Harmonization config is only loaded once
    harmonization = load_configuration(HARMONIZATION_CONF_FILE)

    with CSV.create(tmp_file[0], harmonization=harmonization, **parameters) as reader:
        for _, line in reader:
            event = line.parse()

            if CONFIG.get('destination_pipeline_queue_formatted', False):
                queue_name = CONFIG['destination_pipeline_queue'].format(ev=event)
                destination_pipeline.set_queues(queue_name, "destination")
                destination_pipeline.connect()

            raw_message = MessageFactory.serialize(event)
            destination_pipeline.send(raw_message)
            successful_lines += 1

    return util.create_response('Successfully processed %s lines.' % successful_lines)


@app.route('/uploads/current')
def get_current_upload():
    filename, _ = util.get_temp_file()
    with open(filename, encoding='utf8') as handle:
        resp = util.create_response(handle.read(), content_type='text/csv')
    return resp


def main():
    app.run()


if __name__ == "__main__":
    main()

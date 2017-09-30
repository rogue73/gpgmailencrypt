#!/usr/bin/env python3
import sys
sys.path.insert(1,"../..")

from datetime import datetime
from email.header import decode_header
from itertools import chain
from subprocess import Popen, PIPE
from sys import platform as _platform
from gmeutils.helpers import *
import argparse
import email
import functools
import html
import icalendar
import io
import locale
import logging
import logging.handlers
import mimetypes
import os
import os.path
import pprint
import quopri
import re
import shutil
import sys
import tempfile
import traceback

from PyPDF2 import PdfFileReader, PdfFileWriter
from PyPDF2.generic import NameObject, createStringObject
from bs4 import BeautifulSoup
from requests.exceptions import RequestException
import magic
import requests

_unicodeerror="replace"

assert sys.version_info >= (3, 4)

mimetypes.init()

HEADER_MAPPING = {'Author': 'From',
                  'Title': 'Subject',
                  'X-email2pdf-To': 'To'}

FORMATTED_HEADERS_TO_INCLUDE = [ 'From', 'To', 'Date','Subject']

MIME_TYPES_BLACKLIST = frozenset(['text/html', 'text/plain'])

AUTOCALCULATED_FILENAME_EXTENSION_BLACKLIST = frozenset(['.jpe', '.jpeg'])

AUTOGENERATED_ATTACHMENT_PREFIX = 'floating_attachment'

IMAGE_LOAD_BLACKLIST = frozenset(['emltrk.com', 'trk.email'])

WKHTMLTOPDF_ERRORS_IGNORE = frozenset(
    [r'QFont::setPixelSize: Pixel size <= 0 \(0\)',
    r'Exit with code 1 due to network error: ContentNotFoundError'])

WKHTMLTOPDF_EXTERNAL_COMMAND = 'wkhtmltopdf'


def main(argv, syslog_handler, syserr_handler,parent):
    logger = logging.getLogger('email2pdf')
    warning_count_filter = WarningCountFilter()
    logger.addFilter(warning_count_filter)

    proceed, args = handle_args(argv)

    if not proceed:
        return (False, False)

    if args.enforce_syslog and not syslog_handler:
        raise FatalException("Required syslog socket was not found.")

    if syslog_handler:
        if args.verbose > 0:
            syslog_handler.setLevel(logging.DEBUG)
        else:
            syslog_handler.setLevel(logging.INFO)

    if syserr_handler:
        if args.verbose > 1:
            syserr_handler.setLevel(logging.DEBUG)
        elif args.verbose == 1:
            syserr_handler.setLevel(logging.INFO)
        elif not args.mostly_hide_warnings:
            syserr_handler.setLevel(logging.WARNING)
        else:
            syserr_handler.setLevel(logging.ERROR)

    logger.info("Options used are: " + str(args))

    if not shutil.which(WKHTMLTOPDF_EXTERNAL_COMMAND):
        raise FatalException(
        "email2pdf requires wkhtmltopdf to be installed - please see "
        "https://github.com/andrewferrier/email2pdf/blob/master/README.md#installing-dependencies "
        "for more information.")

    output_directory = os.path.normpath(args.output_directory)

    if not os.path.exists(output_directory):
        raise FatalException("output-directory does not exist.")

    output_file_name = get_output_file_name(args, output_directory)
    logger.info("Output file name is: " + output_file_name)

    set_up_warning_logger(logger, output_file_name)

    input_data = get_input_data(args)
    logger.debug("Email input data is: " + input_data)

    input_email = get_input_email(input_data)
    (payload, parts_already_used) = handle_message_body(args, input_email,parent)
    logger.debug("Payload after handle_message_body: " + str(payload))

    if args.no_remote_links:
        
        remote_links=False
    else:
        remote_links=True

    defaultheader="""<meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
"""
    	
    if args.body:
        payload = remove_invalid_urls(payload,use_externallinks=remote_links)

        if args.headers:
            header_info = get_formatted_header_info(input_email,parent)
            logger.info("Header info is: " + header_info)

            if payload!=None:
                payload = defaultheader+header_info + payload
            else:
                payload=defaultheader+header_info

        logger.debug("Final payload before output_body_pdf: " + payload)
        output_body_pdf(input_email,
                        payload.encode('UTF-8',_unicodeerror),
                        output_file_name)

    if args.attachments:
        number_of_attachments = handle_attachments(input_email,
                                                   output_directory,
                                                   args.add_prefix_date,
                                                   args.ignore_floating_attachments,
                                                   parts_already_used)

    if (not args.body) and number_of_attachments == 0:
        logger.info("First try: didn't print body (on request) or extract any attachments. Retrying with filenamed parts.")
        parts_with_a_filename = filter_filenamed_parts(parts_already_used)
        if len(parts_with_a_filename) > 0:
            number_of_attachments = handle_attachments(input_email,
                                                       output_directory,
                                                       args.add_prefix_date,
                                                       args.ignore_floating_attachments,
                                                       set(parts_already_used - parts_with_a_filename))

        if number_of_attachments == 0:
            logger.warning("Second try: didn't print body (on request) and still didn't find any attachments even when looked for "
                           "referenced ones with a filename. Giving up.")

    if warning_count_filter.warning_pending:
        with open(get_modified_output_file_name(output_file_name, "_original.eml"), 'w') as original_copy_file:
            original_copy_file.write(input_data)

    return (warning_count_filter.warning_pending, args.mostly_hide_warnings)


def handle_args(argv):
    class ArgumentParser(argparse.ArgumentParser):

        def error(self, message):
            raise FatalException(message)

    parser = ArgumentParser(description="Converts emails to PDFs. "
                            "See https://github.com/andrewferrier/email2pdf for more information.", add_help=False)

    parser.add_argument("-i", "--input-file", default="-",
                        help="File containing input email you wish to read in raw form "
                        "delivered from a MTA. If set to '-' (which is the default), it "
                        "reads from stdin.")

    parser.add_argument("--input-encoding",
                        default=locale.getpreferredencoding(), help="Set the "
                        "expected encoding of the input email (whether on stdin "
                        "or specified with the --input-file option). If not set, "
                        "defaults to this system's preferred encoding, which "
                        "is " + locale.getpreferredencoding() + ".")

    parser.add_argument("-o", "--output-file",
                        help="Output file you wish to print the body of the email to as PDF. Should "
                        "include the complete path, otherwise it defaults to the current directory. If "
                        "this option is not specified, email2pdf picks a date & time-based filename and puts "
                        "the file in the directory specified by --output-directory.")

    parser.add_argument("-d", "--output-directory", default=os.getcwd(),
                        help="If --output-file is not specified, the value of this parameter is used as "
                        "the output directory for the body PDF, with a date-and-time based filename attached. "
                        "In either case, this parameter also specifies the directory in which attachments are "
                        "stored. Defaults to the current directory (i.e. " + os.getcwd() + ").")

    parser.add_argument("--overwrite",action="store_true",
                        help="Overwrites the output file, if it already exists")

    parser.add_argument("--no-remote-links",action="store_true",
                        help="if set, content of remote websites will be displayed")

    body_attachment_options = parser.add_mutually_exclusive_group()

    body_attachment_options.add_argument("--no-body", dest='body', action='store_false', default=True,
                                         help="Don't parse the body of the email and print it to PDF, just detach "
                                         "attachments. The default is to parse both the body and detach attachments.")

    body_attachment_options.add_argument("--no-attachments", dest='attachments', action='store_false', default=True,
                                         help="Don't detach attachments, just print the body of the email to PDF.")

    parser.add_argument("--headers", action='store_true',
                        help="Add basic email headers (" + ", ".join(FORMATTED_HEADERS_TO_INCLUDE) +
                        ") to the first PDF page. The default is not to do this.")

    parser.add_argument("--add-prefix-date", action="store_true",
                        help="Prepend an ISO-8601 prefix date (e.g. YYYY-MM-DD-) to any attachment filename "
                        "that doesn't have one. Will search through the whole filename for an existing "
                        "date in that format - if not found, it prepends one.")

    parser.add_argument("--ignore-floating-attachments", action="store_true",
                        help="Emails sometimes contain attachments that don't have a filename and aren't "
                        "embedded in the main HTML body of the email using a Content-ID either. By "
                        "default, email2pdf will detach these and use their Content-ID as a filename, "
                        "or autogenerate a filename. If this option is specified, it will instead ignore "
                        "them.")

    parser.add_argument("--enforce-syslog", action="store_true",
                        help="By default email2pdf will use syslog if available and just log to stderr "
                        "if not. If this option is specified, email2pdf will exit with an error if the syslog socket "
                        "can not be located.")

    verbose_options = parser.add_mutually_exclusive_group()

    verbose_options.add_argument("--mostly-hide-warnings", action="store_true",
                                 help="By default email2pdf will output warnings about handling emails to stderr and "
                                 "exit with a non-zero return code if any are encountered, *as well as* outputting a "
                                 "summary file entitled <output_PDF_name>_warnings_and_errors.txt and the original "
                                 "email as <output_PDF_name>_original.eml. Specifying this option disables the first "
                                 "two, so only the additional files are produced - this makes it easier to use email2pdf "
                                 "if it is run on a schedule, as warnings won't cause the same email to be repeatedly "
                                 "retried.")

    verbose_options.add_argument('-v', '--verbose', action='count', default=0,
                                 help="Make the output more verbose. This affects both the output logged to "
                                 "syslog, as well as output to the console. Using this twice makes it doubly verbose.")

    parser.add_argument('-h', '--help', action='store_true',
                        help="Show some basic help information about how to use email2pdf.")

    args = parser.parse_args(argv[1:])

    assert args.body or args.attachments

    if args.help:
        parser.print_help()
        return (False, None)
    else:
        return (True, args)

def get_input_data(args):
    logger = logging.getLogger("email2pdf")
    logger.debug("System preferred encoding is: " + locale.getpreferredencoding())
    logger.debug("System encoding is: " + str(locale.getlocale()))
    logger.debug("Input encoding that will be used is " + args.input_encoding)

    if args.input_file.strip() == "-":
        data = ""
        input_stream = io.TextIOWrapper(sys.stdin.buffer, encoding=args.input_encoding)

        for line in input_stream:
            data += line

    else:

        with open(args.input_file, "r", encoding=args.input_encoding) as input_handle:
            data = input_handle.read()

    return data


def get_input_email(input_data):
    input_email = email.message_from_string(input_data)
    return input_email


def get_output_file_name(args, output_directory):
    if args.output_file:
        output_file_name = args.output_file

        if os.path.isfile(output_file_name)and not args.overwrite:
            raise FatalException("Output file " + output_file_name + " already exists.")

    else:
        output_file_name = get_unique_version(os.path.join(output_directory,
                                                           datetime.now().strftime("%Y-%m-%dT%H-%M-%S") + ".pdf"))

    return output_file_name


def set_up_warning_logger(logger, output_file_name):
    warning_logger_name = get_modified_output_file_name(output_file_name, "_warnings_and_errors.txt")
    warning_logger = logging.FileHandler(warning_logger_name, delay=True)
    warning_logger.setLevel(logging.WARNING)
    warning_logger.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    logger.addHandler(warning_logger)


def get_modified_output_file_name(output_file_name, append):
    (partial_name, _) = os.path.splitext(output_file_name)
    partial_name = os.path.join(os.path.dirname(partial_name),
                                os.path.basename(partial_name) + append)
    return partial_name


def handle_message_body(args, input_email,parent):
    logger = logging.getLogger("email2pdf")
    cid_parts_used = set()
    part = find_part_by_content_type(input_email, "text/html")

    if part is None:
        part = find_part_by_content_type(input_email, "text/plain")

        if part is not None:
            payload = handle_plain_message_body(part,parent)
        else:
            payload=""
    else:
        (payload, cid_parts_used) = handle_html_message_body(input_email, part,parent)

    appointments=""

    for a in input_email.walk():

        if a.is_multipart():
            continue

        if a.get_content_type()!="text/calendar":
            continue

        appointments+=handle_calendar_body(a,parent)

    attachmentnames=get_all_attachmentnames(input_email,[],parent)  
    attachment=""
    if len(attachmentnames)>0:
    	aname="attachment"
    	if len(attachmentnames)>1:
    		aname="attachments"
    	
    	attachment="<br><br><b><u>%s:</u></b><br><ul>"%localedb(parent,aname)
    	for a in attachmentnames:
    		attachment+="<li>%s</li>"%a
    	attachment+="</ul>"  
    return (payload+appointments+attachment, cid_parts_used)

def handle_calendar_body(part,parent):
    logger = logging.getLogger("email2pdf")
    charset = part.get_content_charset()
    if charset==None:
       part.set_charset("utf8")

    if part['Content-Transfer-Encoding'] == '8bit':
        payload = part.get_payload(decode=False)
        assert isinstance(payload, str)
        logger.info("Email is pre-decoded because Content-Transfer-Encoding is 8bit")
    else:
        is_text=part.get_content_maintype()=="text"
        payload = part.get_payload(decode=False)
    cte=part["Content-Transfer-Encoding"]
    charset = part.get_content_charset()

    if not charset:
        charset = 'utf-8'
        logger.info("Determined email is plain text, defaulting to charset utf-8")
    else:
        logger.info("Determined email is plain text with charset " + str(charset))

    payload=decodetxt(payload,cte,charset)
    cal = icalendar.Calendar.from_ical(payload)
    tbl=""

    for event in cal.walk("VEVENT"):
        organizer=""
        description=""
        summary=""
        t_from=""
        t_to=""
        attendees=[]

        try:
            organizer=event.decoded("ORGANIZER").lower().replace("mailto:","")
        except:
            pass
        try:
            summary=event["Summary"]
        except:
            pass
        try:
            description=event["Description"]
        except:
            pass
        try:
            location=event["Location"]
        except:
            pass
        try:
            datetime="{%(date)s %(time)s}"%{"date":localedb(parent,"_date"),"time":localedb(parent,"_time")}
            t_from=datetime.format(event["DTSTART"].from_ical(event["DTSTART"].to_ical().decode("utf8")))
            t_to=datetime.format(event["DTEND"].from_ical(event["DTSTART"].to_ical().decode("utf8")))
        except:
            pass

        try:
            if isinstance(event.decoded("ATTENDEE"),str):
                attendees.append(event.decoded("ATTENDEE").lower().replace("mailto:",""))
            else:
                for a in event.decoded("ATTENDEE"):
                    attendees.append(a.to_ical().decode("utf8").lower().replace("mailto:",""))
        except:
            pass

        row=("<tr><td style=\"vertical-align:top;background-color: #E6E6FA\">"
        "%(desc)s:</td><td style=\"vertical-align:top;\">%(content)s</td></tr>")
        rowsummary=row%{"desc":localedb(parent,"title"),"content":summary}
        rowdescription=row%{"desc":localedb(parent,"description"),"content":description}
        rowlocation=row%{"desc":localedb(parent,"location"),"content":location}
        rowwhen=row%{"desc":localedb(parent,"when"),"content":"%s - %s</td></tr>"%(t_from,t_to)}
        roworganizer=row%{"desc":localedb(parent,"organizer"),"content":organizer}
        rowattendees=row%{"desc":localedb(parent,"attendees"),"content":"%s </td></tr>"%",<br>".join(attendees)}
        rowone="<tr style=\"border: 1px solid blue;text-align: center; bgcolor:#E6E6FA;padding: 0px;margin: 0px\"><td colspan=2 bgcolor=\"#E6E6FA\" style=\"padding: 0px;margin: 0px\">%(appointment)s</td></tr>"%{"appointment":localedb(parent,"appointment")}
        tbl+=("<table style=\"width:60%; border: 1px solid blue;"
              "text-align: left;padding: 0px;\">"+
                rowone+
                rowsummary+
                rowdescription+
                rowlocation+
                rowwhen+
                roworganizer+
                rowattendees+
              "</table>")
    return tbl

def handle_plain_message_body(part,parent):
    logger = logging.getLogger("email2pdf")

    if part['Content-Transfer-Encoding'] == '8bit':
        payload = part.get_payload(decode=False)
        assert isinstance(payload, str)
        logger.info("Email is pre-decoded because Content-Transfer-Encoding is 8bit")
    else:
        is_text=part.get_content_maintype()=="text"
        payload = part.get_payload(decode=False)
    cte=part["Content-Transfer-Encoding"]
    charset = part.get_content_charset()

    if not charset:
        charset = 'utf-8'
        logger.info("Determined email is plain text, defaulting to charset utf-8")
    else:
        logger.info("Determined email is plain text with charset " + str(charset))

    payload=decodetxt(payload,cte,charset)
    payload = "<html><head><meta charset=\""+charset+ \
    "\"/></head><body><pre>\n" + payload + "\n</pre></body></html>"

    return payload

def handle_html_message_body(input_email, part,parent):
    logger = logging.getLogger("email2pdf")
    cid_parts_used = set()
    is_text=part.get_content_maintype()=="text"
    charset = part.get_content_charset()

    if not charset:
        charset = 'utf-8'

    logger.info("Determined email is HTML with charset " + str(charset))

    if not is_text:
    	payload = html.escape(part.get_payload())
    else:

       if part['Content-Transfer-Encoding'] == '8bit':
            payload=part.get_payload(decode=False)
            payload=payload.encode("UTF8")
       else:
            payload=part.get_payload(decode=True)

    def cid_replace(cid_parts_used, matchobj):
        cid = matchobj.group(1)
        logger.debug("Looking for image for cid " + cid)
        image_part = find_part_by_content_id(input_email, cid)

        if image_part is None:
            image_part = find_part_by_content_type_name(input_email, cid)

        if image_part is not None:
            assert image_part['Content-Transfer-Encoding'] == 'base64'
            image_base64 = image_part.get_payload(decode=False)
            image_base64 = re.sub("[\r\n\t]", "", image_base64)
            image_decoded = image_part.get_payload(decode=True)
            mime_type = get_mime_type(image_decoded)
            cid_parts_used.add(image_part)
            return "data:" + mime_type + ";base64," + image_base64
        else:
            logger.warning("Could not find image cid " + cid + " in email content.")
            return "broken"

    payload = re.sub(r'cid:([\w_@.-]+)', functools.partial(cid_replace, cid_parts_used),
                     str(payload, charset,"replace"))
    return (payload, cid_parts_used)


def output_body_pdf(input_email, payload, output_file_name):
    logger = logging.getLogger("email2pdf")
    wkh2p_process = Popen([WKHTMLTOPDF_EXTERNAL_COMMAND, 
    						'-q', 
    						'--load-error-handling', 'ignore',
                           '--load-media-error-handling', 'ignore', 
                           '--encoding', 'utf-8', '-',
                           output_file_name], stdin=PIPE, stdout=PIPE, stderr=PIPE)
    output, error = wkh2p_process.communicate(input=payload)
    assert output == b''
    stripped_error = str(error, 'utf-8')

    for error_pattern in WKHTMLTOPDF_ERRORS_IGNORE:
        (stripped_error, number_of_subs_made) = re.subn(error_pattern, '', stripped_error)

        if number_of_subs_made > 0:
            logger.debug("Made " + str(number_of_subs_made) + " subs with pattern " + error_pattern)

    original_error = str(error, 'utf-8').rstrip()
    stripped_error = stripped_error.rstrip()

    if wkh2p_process.returncode > 0 and original_error == '':
        logger.debug("wkhtmltopdf failed with exit code " + str(wkh2p_process.returncode) + ", no error output.")
    elif wkh2p_process.returncode > 0 and stripped_error != '':
        logger.debug("wkhtmltopdf failed with exit code " + str(wkh2p_process.returncode) + ", stripped error: " +
                             str(stripped_error, 'utf-8'))
    elif stripped_error != '':
        logger.debug("wkhtmltopdf exited with rc = 0 but produced unknown stripped error output " + stripped_error)

    add_metadata_obj = {}

    for key in HEADER_MAPPING:

        if HEADER_MAPPING[key] in input_email:
            add_metadata_obj[key] = get_utf8_header(input_email[HEADER_MAPPING[key]])

    add_metadata_obj['Producer'] = 'email2pdf'
    add_update_pdf_metadata(output_file_name, add_metadata_obj)


def remove_invalid_urls(payload,use_externallinks=True):
    logger = logging.getLogger("email2pdf")
    try:
        soup = BeautifulSoup(payload, "html5lib")
    except:
        return payload

    for img in soup.find_all('img'):

        if img.has_attr('src'):
            src = img['src']
            lower_src = src.lower()

            if lower_src == 'broken':
                del img['src']
            elif not lower_src.startswith('data'):
                found_blacklist = False

                for image_load_blacklist_item in IMAGE_LOAD_BLACKLIST:

                    if image_load_blacklist_item in lower_src:
                        found_blacklist = True

                if not found_blacklist:
                    logger.debug("Getting img URL " + src)

                    if not can_url_fetch(src,use_externallinks):
                        logger.debug("Could not retrieve img URL " + src + ", replacing with blank.")
                        del img['src']
                else:
                    logger.debug("Removing URL that was found in blacklist " + src)
                    del img['src']
            else:
                logger.debug("Ignoring URL " + src)

    return str(soup)


def can_url_fetch(src,use_externallinks):
    if not use_externallinks:
        return False
    try:
        request = requests.get(src, headers={'Connection': 'close'}, timeout=10)
        # See https://github.com/kennethreitz/requests/issues/1882#issuecomment-44596534
        request.connection.close()
        request.raise_for_status()
        return True
    #except RequestException:
    except:
        return False

def get_all_attachmentnames(input_email, parts_to_ignore,parent):
    attachments=[]
    parts = find_all_attachments(input_email, parts_to_ignore)
    counter=0
    
    for part in parts:
        filename = extract_part_filename(part)

        if not filename:

            if not filename:
                filename = localedb(parent,"file")
                if counter>0:
                    filename+=str(counter)
                counter+=1

            extension = get_type_extension(part.get_content_type())

            if extension:
                filename = filename + extension

        attachments.append(filename)
        attachments.sort()

    return attachments

def handle_attachments(input_email, output_directory, add_prefix_date, ignore_floating_attachments, parts_to_ignore):
    logger = logging.getLogger("email2pdf")
    parts = find_all_attachments(input_email, parts_to_ignore)
    logger.debug("Attachments found by handle_attachments: " + str(len(parts)))

    for part in parts:
        filename = extract_part_filename(part)

        if not filename:

            if ignore_floating_attachments:
                continue

            filename = get_content_id(part)

            if not filename:
                filename = AUTOGENERATED_ATTACHMENT_PREFIX

            extension = get_type_extension(part.get_content_type())

            if extension:
                filename = filename + extension

        assert filename is not None

        if add_prefix_date:

            if not re.search(r"\d\d\d\d[-_]\d\d[-_]\d\d", filename):
                filename = datetime.now().strftime("%Y-%m-%d-") + filename

        logger.info("Extracting attachment " + filename)
        full_filename = os.path.join(output_directory, filename)
        full_filename = get_unique_version(full_filename)
        payload = part.get_payload(decode=True)

        with open(full_filename, 'wb') as output_file:
            output_file.write(payload)

    return len(parts)


def add_update_pdf_metadata(filename, update_dictionary):
    # This seems to be the only way to modify the existing PDF metadata.
    #
    # pylint: disable=protected-access, no-member

    def add_prefix(value):
        return '/' + value

    full_update_dictionary = {add_prefix(k): v for k, v in update_dictionary.items()}

    with open(filename, 'rb') as input_file:
        pdf_input = PdfFileReader(input_file)
        pdf_output = PdfFileWriter()

        for page in range(pdf_input.getNumPages()):
            pdf_output.addPage(pdf_input.getPage(page))

        info_dict = pdf_output._info.getObject()
        info = pdf_input.documentInfo
        full_update_dictionary = dict(chain(info.items(), full_update_dictionary.items()))

        for key in full_update_dictionary:
            assert full_update_dictionary[key] is not None
            info_dict.update({NameObject(key): createStringObject(full_update_dictionary[key])})

        os_file_out, temp_file_name = tempfile.mkstemp(prefix="email2pdf_add_update_pdf_metadata", suffix=".pdf")
        # Immediately close the file as created to work around issue on
        # Windows where file cannot be opened twice.
        os.close(os_file_out)

        with open(temp_file_name, 'wb') as file_out:
            pdf_output.write(file_out)

    shutil.move(temp_file_name, filename)

def extract_part_filename(part):
    logger = logging.getLogger("email2pdf")
    filename = part.get_filename()

    if filename is not None:
        logger.debug("Pre-decoded filename: " + filename)

        if decode_header(filename)[0][1] is not None:
            logger.debug("Encoding: " + str(decode_header(filename)[0][1]))
            logger.debug("Filename in bytes: " + str(decode_header(filename)[0][0]))
            filename = str(decode_header(filename)[0][0], (decode_header(filename)[0][1]))
            logger.debug("Post-decoded filename: " + filename)
        return filename
    else:
        return None

def get_unique_version(filename):
    # From here: http://stackoverflow.com/q/183480/27641
    counter = 1
    file_name_parts = os.path.splitext(filename)

    while os.path.isfile(filename):
        filename = file_name_parts[0] + '_' + str(counter) + file_name_parts[1]
        counter += 1

    return filename

def find_part_by_content_type_name(message, content_type_name):

    for part in message.walk():

        if part.get_param('name', header="Content-Type") == content_type_name:
            return part

    return None

def find_part_by_content_type(message, content_type):

    for part in message.walk():

        if part.get_content_type() == content_type:
            return part

    return None


def find_part_by_content_id(message, content_id):

    for part in message.walk():

        if part['Content-ID'] in (content_id, '<' + content_id + '>'):
            return part

    return None


def get_content_id(part):
    content_id = part['Content-ID']

    if content_id:
        content_id = content_id.lstrip('<').rstrip('>')

    return content_id

# part.get_content_disposition() is only available in Python 3.5+, so this is effectively a backport so we can continue to support
# earlier versions of Python 3. It uses an internal API so is a bit unstable and should be replaced with something stable when we
# upgrade to a minimum of Python 3.5. See http://bit.ly/2bHzXtz.


def get_content_disposition(part):
    value = part.get('content-disposition')

    if value is None:
        return None

    c_d = email.message._splitparam(value)[0].lower()
    return c_d


def get_type_extension(content_type):
    filetypes = set(mimetypes.guess_all_extensions(content_type)) - AUTOCALCULATED_FILENAME_EXTENSION_BLACKLIST

    if len(filetypes) > 0:
        return sorted(list(filetypes))[0]
    else:
        return None


def find_all_attachments(message, parts_to_ignore):
    parts = set()

    for part in message.walk():

        if part not in parts_to_ignore and not part.is_multipart():

            if part.get_content_type() not in MIME_TYPES_BLACKLIST:
                parts.add(part)

    return parts


def filter_filenamed_parts(parts):
    new_parts = set()

    for part in parts:

        if part.get_filename() is not None:
            new_parts.add(part)

    return new_parts


def get_formatted_header_info(input_email,parent):
    header_info = ""

    for header in FORMATTED_HEADERS_TO_INCLUDE:

        if input_email[header]:
            decoded_string = get_utf8_header(input_email[header])
            header_info = header_info + '<b>' + localedb(parent,header.lower()) + '</b>: ' + decoded_string + '<br/>'

    return header_info + '<br/>'

# There are various different magic libraries floating around for Python, and
# this function abstracts that out. The first clause is for `pip3 install
# python-magic`, and the second is for the Ubuntu package python3-magic.


def get_mime_type(buffer_data):

    if 'from_buffer' in dir(magic):
        mime_type = magic.from_buffer(buffer_data, mime=True)

        if type(mime_type) is not str:
            # Older versions of python-magic seem to output bytes for the
            # mime_type name. As of Python 3.6+, it seems to be outputting
            # strings directly.
            mime_type = str(magic.from_buffer(buffer_data, mime=True), 'utf-8')
    else:
        m_handle = magic.open(magic.MAGIC_MIME_TYPE)
        m_handle.load()
        mime_type = m_handle.buffer(buffer_data)

    return mime_type


def get_utf8_header(header):
    # There is a simpler way of doing this here:
    # http://stackoverflow.com/a/21715870/27641. However, it doesn't seem to
    # work, as it inserts a space between certain elements in the string
    # that's not warranted/correct.

    logger = logging.getLogger("email2pdf")
    decoded_header = decode_header(header)
    logger.debug("Decoded header: " + str(decoded_header))
    hdr = ""

    for element in decoded_header:

        if isinstance(element[0], bytes):
            hdr += str(element[0], element[1] or 'ASCII')
        else:
            hdr += element[0]

    return hdr



class WarningCountFilter(logging.Filter):
    # pylint: disable=too-few-public-methods
    warning_pending = False

    def filter(self, record):
        if record.levelno == logging.WARNING:
            self.warning_pending = True
        return True


class FatalException(Exception):

    def __init__(self, value):
        Exception.__init__(self, value)
        self.value = value

    def __str__(self):
        return repr(self.value)


def call_main(argv, syslog_handler, syserr_handler):
    # pylint: disable=bare-except
    logger = logging.getLogger("email2pdf")

    try:
        (warning_pending, mostly_hide_warnings) = main(argv, syslog_handler, syserr_handler)
    except FatalException as exception:
        logger.error(exception.value)
        sys.exit(2)
    except:
        traceback.print_exc()
        sys.exit(3)

    if warning_pending and not mostly_hide_warnings:
        sys.exit(1)


if __name__ == "__main__":
    logger_setup = logging.getLogger("email2pdf")
    logger_setup.propagate = False
    logger_setup.setLevel(logging.DEBUG)
    syserr_handler_setup = logging.StreamHandler(stream=sys.stderr)
    syserr_handler_setup.setLevel(logging.WARNING)
    syserr_formatter = logging.Formatter('%(levelname)s: %(message)s')
    syserr_handler_setup.setFormatter(syserr_formatter)
    logger_setup.addHandler(syserr_handler_setup)

    if _platform == "linux" or _platform == "linux2":
        SYSLOG_ADDRESS = '/dev/log'
    elif _platform == "darwin":
        SYSLOG_ADDRESS = '/var/run/syslog'
    else:
        logger_setup.warning("I don't know this platform (" + _platform + "); cannot log to syslog.")
        SYSLOG_ADDRESS = None

    if SYSLOG_ADDRESS and os.path.exists(SYSLOG_ADDRESS):
        syslog_handler_setup = logging.handlers.SysLogHandler(address=SYSLOG_ADDRESS)
        syslog_handler_setup.setLevel(logging.INFO)
        SYSLOG_FORMATTER = logging.Formatter('%(pathname)s[%(process)d] %(levelname)s %(lineno)d %(message)s')
        syslog_handler_setup.setFormatter(SYSLOG_FORMATTER)
        logger_setup.addHandler(syslog_handler_setup)
    else:
        syslog_handler_setup = None

    call_main(sys.argv, syslog_handler_setup, syserr_handler_setup)

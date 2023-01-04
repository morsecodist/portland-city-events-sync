import logging
import os
import re
import pytz
from datetime import datetime, timedelta, timezone
from tempfile import NamedTemporaryFile
from time import sleep

import openai
import pdfplumber
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By

from google_calendar import upsert_event, next_n_events


HOME_URL = os.environ['HOME_URL']

DEFAULT_DESCRIPTION = "No agenda yet"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


firefox_options = webdriver.FirefoxOptions()
firefox_options.headless = True

outline_format = """• [Insert Level One Text]
    • [Insert Level Two Text]
        • [Insert Level Three Text]
"""

def summarize_text(pages):
    openai.api_key = os.getenv("OPEN_API_SECRET")
    for text in pages:
        prompt = f"To summarize the following page:\n{text}\n\nUsing this format:\n{outline_format}\n"
        response = openai.Completion.create(
            engine="text-davinci-003",
            prompt=prompt,
            # prompt="Please summarize, as a bulleted list, the agendas of the following meetings, excluding any items that are present in every meeting (e.g. 'review of previous meeting minutes').\n" + text,
            temperature=0.7,
            max_tokens=400,
            top_p=1.0,
            frequency_penalty=0.0,
            presence_penalty=1,
        )
        response_text = response["choices"][0]["text"]
        yield response_text


meetings = {(event['summary'], event['start']['dateTime']): event for event in next_n_events()}


with webdriver.Firefox(options=firefox_options) as browser:
    def wait_for_url():
        current_url = browser.current_url
        while current_url == 'about:blank':
            current_url = browser.current_url
        return current_url

    browser.get(HOME_URL)
    ny_timezone = timezone(timedelta(hours=-5))
    eastern_timezone = pytz.timezone("America/New_York")
    sleep(3)
    table = browser.find_element(value="aspxroundpanelCurrent_pnlDetails_grdEventsCurrent")
    rows = table.find_elements(by=By.CLASS_NAME, value="dxgvDataRow_CustomThemeModerno")
    for row in rows:
        row.find_element(by=By.TAG_NAME, value="a").click()
        browser.switch_to.window(browser.window_handles[-1])
        agenda_link = wait_for_url()
        browser.close()
        browser.switch_to.window(browser.window_handles[0])

        cells = row.find_elements(by=By.TAG_NAME, value="td")
        title = cells[1].text.strip()
        start_time = datetime.strptime(cells[2].text, "%m/%d/%Y %I:%M %p")
        start_time = start_time.replace(tzinfo=ny_timezone)
        logger.info(f"processing: '{title}' {start_time}")
        download_cell = cells[4]
        downloads = download_cell.find_elements(by=By.CLASS_NAME, value="dxeButton")
        description = DEFAULT_DESCRIPTION
        zoom_link = None
        existing_meeting = meetings.get((title, start_time.isoformat()))
        if downloads and (not existing_meeting or existing_meeting['description'] == 'No agenda yet'):
            downloads[0].click()
            [elem for elem in download_cell.find_elements(by=By.TAG_NAME, value="tr") if elem.text == "Agenda"][0].click()
            browser.switch_to.window(browser.window_handles[-1])
            current_url = wait_for_url()
            with NamedTemporaryFile('wb', suffix=".pdf") as f:
                f.write(requests.get(current_url).content)
                pages = pdfplumber.open(f.name).pages

            text_pages = [page.extract_text() for page in pages]
            raw_text = "\n".join(text_pages)

            zoom_link_regex = r"https?://[a-z0-9.-]*\.zoom\.us/\S+"
            zoom_link = re.search(zoom_link_regex, raw_text).group()

            summary = "\n".join(summarize_text([raw_text]))
            description = f"Agenda Link: {agenda_link}\n\n"
            if zoom_link:
                description += "Meeting Link: {zoom_link}\n\n"
            description += "Agenda Abridged:\n{summary}"
            browser.close()
            browser.switch_to.window(browser.window_handles[0])
        if not existing_meeting or not existing_meeting.get('description') or (existing_meeting['description'] == DEFAULT_DESCRIPTION and description != DEFAULT_DESCRIPTION):
            upsert_event(title, start_time, start_time + timedelta(hours=2), description, existing_meeting)
            logging.info(f"updating: '{title}' {start_time}")
        else:
            logging.info(f"skipping: '{title}' {start_time}")

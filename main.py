import asyncio
import io
import logging
import os
import pytz
import re
from datetime import datetime, timedelta, timezone
from typing import List, NamedTuple

import aiohttp
import markdown
import openai
import pdfplumber
from google_calendar import upsert_event, next_n_events


OPEN_AI_SECRET = os.environ['OPEN_API_SECRET']
CITY_API_BASE_URL = os.environ['CITY_API_BASE_URL']

DEFAULT_DESCRIPTION = "No agenda yet"

class Event(NamedTuple):
    raw_data: dict
    name: str
    start_time: datetime
    end_time: datetime
    agenda_link: str | None
    zoom_link: str | None
    summary: str | None
    description: str | None

demo = """
# Name of Meeting in Title Case Followed by Agenda

## Category Heading in Title Case Without Numbers
- Agenda items in a nested bulleted list
  - Sub-item one
  - Sub-item two

## Next Category Heading in Title Case
- Agenda items in a nested bulleted list
  - Sub-item one
  - Sub-item two

## Adjournment
"""


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


meetings = {(event['summary'], event['start']['dateTime']): event for event in next_n_events()}

async def get_events_from_date(date: datetime):
    date_str = date.strftime("%Y-%m-%d")
    url = f"{CITY_API_BASE_URL}/Events?$filter=+startDateTime+ge+{date_str}&$orderby=startDateTime"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return (await response.json())['value']


def get_file_download_link(file_id: int) -> str:
    return f"{CITY_API_BASE_URL}/Meetings/GetMeetingFileStream(fileId={file_id},%20plainText=false)"


async def download_and_parse_pdf(url: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                return extract_text_from_pdf(io.BytesIO(await response.read()))
            else:
                print(f"Error: {response.status}")
                return None


def extract_text_from_pdf(data: io.BytesIO):
    with pdfplumber.open(data) as pdf:
        text = ""
        for page in pdf.pages:
            text += page.extract_text()
            text += "\n"
        return text


def get_agenda_file_id(event: dict):
    agendas = [file for file in event['publishedFiles'] if file['type'] == 'Agenda']
    if not agendas:
        return None
    return int(agendas[0]['fileId'])


async def get_agenda_summary(agenda_text):
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPEN_AI_SECRET}"
    }
    prompt = f"Generate a simple to understand bullet-point summary in Markdown format for the following city meeting agenda, focusing on the main agenda items and ignoring boilerplate information such as the date, how to submit public comments, remote information. The summary should be preserve the categories of the items and resemble the following format:\n\n {demo}\n\n Here is the agenda to summarize:\n\n{agenda_text}\n\nSummary:\n"
    data = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": prompt }],
        "temperature": 0.7,
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(url, json=data) as response:
            if response.status == 200:
                response_json = await response.json()
                return response_json['choices'][0]['message']['content'].strip()
            else:
                print(f"Error: {response.status}")
                print(await response.text())
                return None


async def build_event(event: dict) -> Event:
    name = event['eventName']
    start_time = datetime.strptime(event['startDateTime'], '%Y-%m-%dT%H:%M:%SZ')
    tz = timezone(pytz.timezone("America/New_York").utcoffset(datetime.utcnow()))
    start_time = start_time.replace(tzinfo=tz)
    end_time = start_time + timedelta(hours=2)
    logger.info(f"processing: '{name}' {start_time} - {end_time}")
    existing_meeting = meetings.get((name, start_time.isoformat()))

    agenda_file_id = get_agenda_file_id(event)
    agenda_link = None
    zoom_link = None
    summary = None
    description = DEFAULT_DESCRIPTION
    if agenda_file_id and (not existing_meeting or DEFAULT_DESCRIPTION in existing_meeting['description']):
        agenda_link = get_file_download_link(agenda_file_id)
        agenda_text = await download_and_parse_pdf(agenda_link)
        zoom_link_regex = r"https?://[a-z0-9.-]*\.zoom\.us/\S+/\d+"
        zoom_link_result = re.search(zoom_link_regex, agenda_text)
        zoom_link = zoom_link_result and zoom_link_result.group()
        summary = await get_agenda_summary(agenda_text)
        description = f"## [View Full Agenda]({agenda_link})\n"
        if zoom_link:
            description += f"## [Join Meeting]({zoom_link})\n"
        else:
            description += "## Link to Join Unavailable\n"
        description += summary
        description = markdown.markdown(description)

    return Event(event, name, start_time, end_time, agenda_link, zoom_link, summary, description)


async def main():
    raw_events = await get_events_from_date(datetime.now())
    events: List[Event] = await asyncio.gather(*[build_event(event) for event in raw_events])

    for event in events:
        existing_meeting = meetings.get((event.name, event.start_time.isoformat()))
        if not existing_meeting or not existing_meeting.get('description') or (DEFAULT_DESCRIPTION in existing_meeting['description'] and event.description != DEFAULT_DESCRIPTION):
            upsert_event(event.name, event.start_time, event.end_time, event.description, existing_meeting)
            logging.info(f"updating: '{event.name}' {event.start_time}")
        else:
            logging.info(f"skipping: '{event.name}' {event.start_time}")


if __name__ == "__main__":
    asyncio.run(main())


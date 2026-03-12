# Psych Ingestor
A simple API endpoint for ingesting, routing, and saving data from behavioral tasks in the psych research world

## Overview
The Psych Ingestor (PI) is set up to handle data streams from a variety of online studies -- in principle, all of your tasks will share this service. You'll set up a file containing a set of task definitions, and your tasks will send HTTPS requests to it to signal start and end of tasks, as well as send data. Data is sent as a series of JSON lines (one JSON object per recorded event) and stored with minimal processing.

Your task itself will generally be statically-hostable; it will normally consist of HTML, javascript, and CSS. There's nothing saying you _can't_ have a separate server-side component to tihs, but you don't need one.

PI supports parameter signing, so you can verify that a participant has come from the link you originally sent.

Technically PI consists of a few components:

- A Python / FastAPI service that handles data collection and storage
- Somewhere on the server for you to store data for each study
- A database containing configuration information for studies and participants
- A command-line program to edit the configuration 

## Task configuration

Configuration includes things such as:
- What parameters PI expects from the link to define the session
- Do you allow submitting multiple datasets per session
- Where data should be stored
- How data should be named
- What (if any) configuration parameters PI will send back to the task
- Are participants / sessions predefined?
- What to do if a dataset isn't finalized
- Is the task accepting more data or is it currently closed

## Command-line program

Instead of a web interface for configuring things, we'll have a CLI that 

## HTTPS endpoints

### `GET /health`

Returns some useful HTTP status information on things

### `POST /study/<study>/sessions/new`



### `POST /session/<session_id>/data`

Takes a 

## Technology
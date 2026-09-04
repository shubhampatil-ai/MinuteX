# MinuteX

**AI-powered meeting recording, transcription, and productivity platform.**

MinuteX records meetings from a mobile app or dedicated ESP32 device, converts audio into text, and uses AI to generate summaries, highlights, documents, tasks, and insights.

It is designed to support both **individual users** and **organisations working across multiple projects and teams**.

---

## What MinuteX Does

MinuteX turns a meeting recording into actionable information:

```text
Record Meeting
      ↓
Upload Audio
      ↓
Transcription
      ↓
AI Analysis
      ↓
Summary + Highlights + Tasks + Insights
      ↓
Project Workspace
      ↓
CRM / Team Workflow
```

### Key capabilities

* 🎙️ Meeting recording from mobile, uploaded files, or MinuteX device
* 📝 Automatic transcription with speaker identification
* 🤖 AI summaries, highlights, documents and Q&A
* ✅ Automatic task extraction
* 👤 Contact and participant management
* 📁 Project-based meeting organisation
* 👥 Organisation and project members
* 🔗 CRM integration
* 🔍 Search and filtering across meetings and tasks
* 📤 Export and sharing of meeting information

---

# Architecture

MinuteX is built as three main components:

```text
┌─────────────────────┐
│     Mobile App      │
│ React Native / Expo │
└──────────┬──────────┘
           │
           ↓
┌─────────────────────┐
│      AWS Cloud      │
│                     │
│ API + Lambda        │
│ S3 + DynamoDB       │
│ AI Processing       │
└──────────┬──────────┘
           │
           ↓
┌─────────────────────┐
│ AI Services         │
│                     │
│ ElevenLabs Scribe   │
│ Groq                │
└─────────────────────┘
           │
           ↓
      CRM / Users
```

### Technology

| Area           | Technology               |
| -------------- | ------------------------ |
| Mobile         | React Native + Expo      |
| Backend        | AWS Lambda + API Gateway |
| Database       | DynamoDB                 |
| Storage        | Amazon S3                |
| Speech-to-text | ElevenLabs Scribe        |
| AI             | Groq                     |
| CRM            | Salesforce               |
| Hardware       | ESP32-S3                 |

All AWS resources currently run in **`ap-south-1`**.

---

# Organisation & Project Architecture

MinuteX supports two working contexts:

### Personal workspace

A user can continue using MinuteX independently.

```text
User
 ├── Meetings
 ├── Contacts
 ├── Tasks
 └── Devices
```

### Organisation workspace

Teams can collaborate through organisations and projects.

```text
Organisation
│
├── Members
│
├── Projects
│   │
│   ├── Project Members
│   ├── Meetings
│   ├── Contacts
│   └── Tasks
│
└── CRM
```

A user can belong to multiple organisations and multiple projects.

### Roles

| Role    | Responsibility                                |
| ------- | --------------------------------------------- |
| Owner   | Full organisation control                     |
| Manager | Manage projects, members and operational data |
| Member  | Work within assigned projects                 |

The permission model follows:

```text
User
 ↓
Organisation Membership
 ↓
Role
 ↓
Project Membership
 ↓
Resource Access
```

This keeps organisation data separated while allowing controlled collaboration.

---

# Meetings

Every meeting follows the same processing pipeline regardless of where it was recorded.

```text
Mobile
   │
Upload
   │
Device ───┐
          ├──→ S3
File ─────┘
            ↓
       Transcription
            ↓
         AI Analysis
            ↓
        Meeting Data
```

A meeting can belong to a project or remain in the user's personal workspace.

The original transcript remains the source of truth.

---

# AI Workspace

After processing, users see an AI-powered meeting workspace instead of only a raw transcript.

```text
Audio
 ↓
Executive Summary
 ↓
Meeting Highlights
 ↓
AI Documents
 ↓
Quick AI
 ↓
Ask MinuteX
 ↓
Transcript
```

AI capabilities include:

* Executive summaries
* Meeting minutes
* Action items
* Follow-up emails
* WhatsApp summaries
* Sales reports
* Site visit reports
* Customer requirement reports
* Decisions
* Deadlines
* Risks
* Budget information
* Customer requirements
* Timeline extraction
* Meeting Q&A

AI results are cached and regenerated only when required.

---

# Contacts & Tasks

MinuteX maintains a central contact identity instead of creating duplicate people for every project.

```text
Contact
   ↓
Project(s)
   ↓
Meeting(s)
   ↓
Tasks
```

Tasks are first-class objects and can be viewed across meetings and projects.

The system does **not automatically guess a person's identity** when names are ambiguous.

For example:

```text
"Rahul will send the proposal."

        ↓

Possible Rahuls found

        ↓

User selects the correct person
```

This prevents tasks from being assigned to the wrong person.

---

# CRM Integration

CRM integration is designed around both the organisation and the individual user.

```text
Organisation
      ↓
Project CRM Configuration
      ↓
CRM Contacts / Records
      ↓
MinuteX Project
```

The user's CRM identity is used when MinuteX needs to perform an action on behalf of that user.

CRM contacts can be brought into a MinuteX project without automatically making them MinuteX users.

---

# Repository Structure

```text
MinuteX/
│
├── cloud/
│   ├── functions/
│   │   ├── userapi/
│   │   ├── transcribe/
│   │   └── device-presign/
│   │
│   ├── shared/
│   ├── scripts/
│   └── tests/
│
├── app/
│   └── React Native / Expo application
│
├── device/
│   └── ESP32-S3 firmware
```

---

# Data Model

At a high level, MinuteX uses:

```text
Organisation
    │
    ├── Members
    │
    └── Projects
          │
          ├── Members
          ├── Meetings
          │      ├── Transcript
          │      ├── AI Output
          │      ├── Participants
          │      └── Tasks
          │
          └── Contacts
```

DynamoDB is used for application data and S3 is used for audio and large transcript data.

---

# Running the Project

## Backend tests

```bash
python -m pytest cloud/tests/ -q
```

The test suite uses mocks and does not require AWS credentials for normal unit testing.

## Firmware

```bash
cd device
pio run
```

To flash the device:

```bash
pio run -t upload
```

## Environment

Copy the example configuration:

```bash
cp .env.example .env
```

Configure the required AWS and application settings before deployment.

---

# Deployment

The backend is deployed using the scripts under:

```text
cloud/scripts/
```

These scripts provision and deploy AWS resources such as:

* DynamoDB
* Lambda
* API Gateway
* S3
* IAM

The mobile application is maintained as a React Native / Expo application under `app/`.

---

# Documentation

The main README intentionally contains only the information needed to understand and work with MinuteX.

Detailed engineering information should be maintained separately:

```text
docs/
├── architecture/
├── database/
├── api/
├── ai/
├── crm/
├── device/
├── deployment/
└── migrations/
```

Examples of topics that belong in detailed documentation:

* DynamoDB keys and GSIs
* API route specifications
* IAM policies
* Migration procedures
* AI prompts and caching
* Device pairing protocol
* Transcript storage
* CRM synchronization
* Production troubleshooting

---

# Current Product Direction

MinuteX is evolving from a **personal meeting recorder** into a **team-oriented AI meeting workspace**.

The target structure is:

```text
                    MinuteX
                       │
          ┌────────────┴────────────┐
          │                         │
      Personal                  Organisation
      Workspace                  Workspace
                                    │
                              ┌─────┴─────┐
                              │           │
                           Projects     Members
                              │
                ┌─────────────┼─────────────┐
                │             │             │
             Meetings      Contacts       Tasks
                │
             AI Insights
                │
              CRM
```

The goal is to keep the existing recording and AI pipeline stable while adding organisation, project, collaboration and CRM capabilities around it.

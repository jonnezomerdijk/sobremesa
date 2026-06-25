# Sobremesa

Landing page + group matching backend for Sobremesa dinner events.

## Setup

```bash
cd sobremesa
pip install -r requirements.txt
uvicorn main:app --reload
```

Then open http://localhost:8000

## Structure

```
sobremesa/
├── main.py          # FastAPI app — routes, DB
├── matching.py      # Group matching algorithm
├── templates/
│   └── index.html   # Landing page + signup form
└── requirements.txt
```

The SQLite DB (`sobremesa.db`) is created automatically on first run.

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Landing page |
| POST | `/signup` | Register a new person |
| GET | `/admin/signups` | List all signups |
| POST | `/admin/match` | Run matching, form groups |
| GET | `/admin/groups` | List formed groups |

### Signup payload

```json
{
  "name": "Elena",
  "email": "elena@example.com",
  "neighbourhood": "Rathmines",
  "dietary": ["vegetarian"],
  "availability": ["saturday-evening", "sunday-afternoon"]
}
```

## Matching logic

Groups are formed in two passes:
1. **Neighbourhood-first**: people in the same neighbourhood are grouped together (4–8 per group).
2. **Cross-neighbourhood**: leftovers are paired by availability overlap + proximity score.

Groups with fewer than 4 people are folded into existing groups if possible, otherwise left unmatched for the next run.

Run matching via:
```bash
curl -X POST http://localhost:8000/admin/match
```

## Next steps

- Email notifications when a group is formed (e.g. with Resend or SendGrid)
- Admin dashboard UI instead of raw JSON endpoints
- Group chat / intro email template
- Host/guest designation per group

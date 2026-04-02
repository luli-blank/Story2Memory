# MySQL Long Context Memory Schema

This folder stores the MySQL schema used by long-context chat memory.

## Tables

### `sessions`
- `id`: conversation id (UUID string)
- `user_id`: user id
- `title`: optional session title
- `current_summary`: rolling compressed summary
- `last_summarized_msg_id`: last message id included in `current_summary`
- `created_at` / `updated_at`

### `messages`
- `id`: auto increment primary key
- `session_id`: FK to `sessions.id`
- `role`: `user` / `assistant` / `system`
- `content`: message content
- `token_count`: token usage estimate for this row
- `created_at`

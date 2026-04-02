.PHONY: run test lint docker-build docker-up docker-down docker-logs docker-smoke init-db init-neo4j

run:
	reflex run --env dev

test:
	pytest -q

lint:
	ruff check app core database reflex_app tests

docker-build:
	docker compose up --build --no-start

docker-up:
	cp -n .env.example .env || true
	docker compose up --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f app

docker-smoke:
	bash scripts/ci_public_smoke.sh

init-db:
	docker compose exec -T mysql sh -lc 'mysql -uroot -p"$$MYSQL_ROOT_PASSWORD" novel_cognition' < database/mysql/create_tables.sql

init-neo4j:
	docker compose exec -T neo4j sh -lc '(command -v cypher-shell >/dev/null 2>&1 && cypher-shell -u "$${NEO4J_AUTH%%/*}" -p "$${NEO4J_AUTH##*/}") || ([ -x /var/lib/neo4j/bin/cypher-shell ] && /var/lib/neo4j/bin/cypher-shell -u "$${NEO4J_AUTH%%/*}" -p "$${NEO4J_AUTH##*/}") || ([ -x /usr/bin/cypher-shell ] && /usr/bin/cypher-shell -u "$${NEO4J_AUTH%%/*}" -p "$${NEO4J_AUTH##*/}")' < scripts/init_neo4j.cypher

#!/usr/bin/env bash
# fetch-profiles.sh - тянет LinkedIn-профілі через HarvestAPI (Apify actor).
#
# Один endpoint: Apify actor harvestapi/linkedin-profile (run-sync-get-dataset-items).
# Ключ Apify бере зі змінної APIFY_TOKEN (юзер дає свій).
#
# Використання:
#   APIFY_TOKEN=apify_api_xxx ./fetch-profiles.sh leads.txt out_dir/
#     leads.txt - список LinkedIn-URL кандидатів, по одному в рядок
#     out_dir/  - куди класти JSON кожного профілю (cand-1.json, cand-2.json, ...)
#
# Кожен профіль зберігається окремим JSON-файлом. Помилку по одному посиланню
# не валить решту - пише stderr і йде далі.

set -uo pipefail

LEADS="${1:?Usage: fetch-profiles.sh <leads.txt> <out_dir>}"
OUT="${2:?Usage: fetch-profiles.sh <leads.txt> <out_dir>}"
TOKEN="${APIFY_TOKEN:?Set APIFY_TOKEN with your Apify/HarvestAPI key}"

ACTOR="harvestapi~linkedin-profile"
BASE="https://api.apify.com/v2/acts/${ACTOR}/run-sync-get-dataset-items"
TIMEOUT="${APIFY_TIMEOUT:-180}"

mkdir -p "$OUT"

i=0
ok=0
fail=0
while IFS= read -r url || [[ -n "$url" ]]; do
  url="$(echo "$url" | tr -d '\r' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  [[ -z "$url" ]] && continue
  [[ "$url" == \#* ]] && continue
  i=$((i+1))
  out="$OUT/cand-$i.json"
  echo "[$i] fetch: $url" >&2

  # HarvestAPI profile actor: вхід profileUrls (масив). Повертає dataset-масив,
  # беремо перший елемент = профіль.
  body="$(jq -n --arg u "$url" '{profileScraperMode:"Full", profileUrls:[$u]}')"
  resp="$(curl --max-time "$TIMEOUT" -fsS -X POST \
            "$BASE?token=$TOKEN" \
            -H "Content-Type: application/json" \
            -d "$body" 2>/dev/null)"

  if [[ -z "$resp" ]] || ! echo "$resp" | jq -e 'if type=="array" then length>0 else true end' >/dev/null 2>&1; then
    echo "    FAIL: пустой/ошибочный ответ для $url" >&2
    echo "{\"_fetch_failed\":true,\"url\":\"$url\"}" > "$out"
    fail=$((fail+1))
    sleep 1
    continue
  fi

  # нормализуем: если массив - берём [0], иначе как есть
  echo "$resp" | jq 'if type=="array" then .[0] else . end' > "$out"
  ok=$((ok+1))
  sleep 1   # лёгкий троттлинг между профилями
done < "$LEADS"

echo "" >&2
echo "Готово: $ok успешно, $fail не достали, всего $i ссылок → $OUT" >&2

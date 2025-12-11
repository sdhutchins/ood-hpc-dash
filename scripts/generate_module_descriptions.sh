#!/bin/bash
# Generate module descriptions and update config/module_categories.json

OUTPUT_FILE="config/module_categories.json"

[ -f /usr/share/lmod/lmod/init/bash ] && source /usr/share/lmod/lmod/init/bash || \
[ -f /etc/profile.d/modules.sh ] && source /etc/profile.d/modules.sh

# Get families
module -t spider 2>&1 | awk -F'/' 'NF >= 2 {
    if (NF > 2) {
        family = ""
        for (i = 1; i < NF; i++) {
            if (i > 1) family = family "/"
            family = family $i
        }
    } else {
        family = $1
    }
    print family
}' | sort -u > /tmp/families.txt

# Fetch descriptions
> /tmp/descriptions.txt
while IFS= read -r family; do
    output=$(timeout 5 bash -c "module --redirect spider '$family' 2>&1" 2>/dev/null || echo "")
    desc=""
    in_desc=false
    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*Description: ]] && desc="${line#*Description: }" && in_desc=true && continue
        [ "$in_desc" = true ] && [[ "$line" =~ ^[[:space:]]*Dependencies: ]] && break
        [ "$in_desc" = true ] && desc="${desc} ${line}"
    done <<< "$output"
    desc=$(echo "$desc" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g')
    echo "Generated description for $family" >&2
    echo "\"$family\": \"$desc\""
done < /tmp/families.txt > /tmp/descriptions.txt

# Update JSON
categories=$(jq 'if .categories then .categories else del(.descriptions) // {} end' "$OUTPUT_FILE" 2>/dev/null || echo '{}')
descriptions=$(cat /tmp/descriptions.txt | jq -s 'from_entries')
echo "$categories" | jq --argjson d "$descriptions" '{categories: ., descriptions: $d}' > "$OUTPUT_FILE"

rm -f /tmp/families.txt /tmp/descriptions.txt

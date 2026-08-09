#!/bin/bash
# One-time setup: creates a local self-signed "Chatter Local Dev" code
# signing certificate in the login keychain and trusts it for code signing.
#
# Ad-hoc signing (codesign -s -) has no stable identity, so macOS ties
# Accessibility/Microphone/Input Monitoring TCC grants to the exact binary
# hash — every rebuild silently invalidates permissions already granted in
# System Settings, even though the switch still shows on. Signing with this
# certificate instead gives TCC a stable designated requirement, so grants
# made once survive future rebuilds. build_app.sh picks it up automatically
# once it exists.
set -euo pipefail

IDENTITY="Chatter Local Dev"

if security find-identity -v -p codesigning | grep -q "$IDENTITY"; then
    echo "'$IDENTITY' already exists in the login keychain. Nothing to do."
    exit 0
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

cat > "$WORKDIR/codesign.cnf" <<EOF
[ req ]
default_bits       = 2048
prompt             = no
distinguished_name = dn
x509_extensions    = ext

[ dn ]
CN = $IDENTITY

[ ext ]
basicConstraints        = critical, CA:false
keyUsage                = critical, digitalSignature
extendedKeyUsage        = critical, codeSigning
subjectKeyIdentifier    = hash
EOF

echo "Generating self-signed certificate..."
openssl req -x509 -newkey rsa:2048 -keyout "$WORKDIR/dev.key" -out "$WORKDIR/dev.crt" \
    -days 3650 -nodes -config "$WORKDIR/codesign.cnf" -extensions ext

# Modern OpenSSL defaults its PKCS#12 export to an algorithm macOS's Security
# framework cannot import; -legacy keeps it importable.
openssl pkcs12 -export -out "$WORKDIR/dev.p12" -inkey "$WORKDIR/dev.key" -in "$WORKDIR/dev.crt" \
    -passout pass:chatter -legacy

echo "Importing into the login keychain..."
security import "$WORKDIR/dev.p12" -k ~/Library/Keychains/login.keychain-db -P chatter \
    -T /usr/bin/codesign -T /usr/bin/security

echo "Trusting the certificate for code signing..."
security add-trusted-cert -p codeSign -k ~/Library/Keychains/login.keychain-db "$WORKDIR/dev.crt"

security find-identity -v -p codesigning | grep "$IDENTITY"
echo "Done. packaging/build_app.sh will now sign with '$IDENTITY'."
echo "Existing permission grants for the old ad-hoc-signed Chatter.app will not"
echo "carry over — re-grant Accessibility/Microphone/Input Monitoring once more"
echo "after the next build. Future rebuilds will keep the grant."

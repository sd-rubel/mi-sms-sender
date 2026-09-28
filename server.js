const { default: makeWASocket, useMultiFileAuthState, DisconnectReason } = require('@whiskeysockets/baileys');
const qrcode = require('qrcode-terminal');
const http = require('http');
const pino = require('pino');

let sock = null;
let isConnected = false;

async function connectToWhatsApp() {
    const { state, saveCreds } = await useMultiFileAuthState('wa_auth_session');

    sock = makeWASocket({
        auth: state,
        logger: pino({ level: 'silent' }),
        printQRInTerminal: false,
        syncFullHistory: false
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            console.log('\n📱 আপনার WhatsApp থেকে নিচের QR কোডটি স্ক্যান করুন:\n');
            qrcode.generate(qr, { small: true });
        }

        if (connection === 'close') {
            isConnected = false;
            const shouldReconnect = (lastDisconnect?.error)?.output?.statusCode !== DisconnectReason.loggedOut;
            if (shouldReconnect) connectToWhatsApp();
            else console.log('❌ WhatsApp লগআউট হয়ে গেছে।');
        } else if (connection === 'open') {
            isConnected = true;
            console.log('✅ WhatsApp সফলভাবে কানেক্টেড আছে!');
        }
    });
}

const server = http.createServer(async (req, res) => {
    res.setHeader('Content-Type', 'application/json; charset=utf-8');

    if (req.method === 'GET' && req.url === '/status') {
        return res.end(JSON.stringify({ connected: isConnected }));
    }

    let body = '';
    req.on('data', chunk => { body += chunk.toString(); });
    req.on('end', async () => {
        try {
            if (!isConnected || !sock) {
                return res.end(JSON.stringify({ ok: false, error: 'WhatsApp কানেক্ট নেই' }));
            }
            const data = JSON.parse(body || '{}');

            if (req.method === 'POST' && req.url === '/check') {
                const results = await sock.onWhatsApp(data.phone);
                const result = results && results[0];
                if (result && result.exists) {
                    return res.end(JSON.stringify({ ok: true, exists: true, jid: result.jid }));
                } else {
                    return res.end(JSON.stringify({ ok: true, exists: false, jid: null }));
                }
            }

            if (req.method === 'POST' && req.url === '/send') {
                const jid = data.jid || `${data.to}@s.whatsapp.net`;
                await sock.sendMessage(jid, { text: data.message });
                return res.end(JSON.stringify({ ok: true }));
            }

            res.writeHead(404);
            res.end(JSON.stringify({ ok: false }));
        } catch (err) {
            res.end(JSON.stringify({ ok: false, error: err.message }));
        }
    });
});

server.listen(3000, '127.0.0.1', () => {
    connectToWhatsApp();
});


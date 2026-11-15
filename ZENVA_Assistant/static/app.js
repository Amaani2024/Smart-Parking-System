
const el = id => document.getElementById(id);

const ui = {
  connectionDot: el("connectionDot"),
  connectionText: el("connectionText"),
  assistantState: el("assistantState"),
  assistantMessage: el("assistantMessage"),
  robot: el("robot"),
  micButton: el("micButton"),
  micText: el("micText"),
  liveTranscript: el("liveTranscript"),
  questionInput: el("questionInput"),
  askButton: el("askButton"),
  available: el("available"),
  occupied: el("occupied"),
  reserved: el("reserved"),
  inside: el("inside"),
  slots: el("slots"),
  updated: el("updated"),
  temperature: el("temperature"),
  humidity: el("humidity"),
  gas: el("gas"),
  relay: el("relay"),
  gate: el("gate"),
  gateStatus: el("gateStatus"),
  gateCommand: el("gateCommand"),
  eventPlate: el("eventPlate"),
  eventStatus: el("eventStatus"),
  eventSlot: el("eventSlot"),
};

let recognition = null;
let isListening = false;
let lastAnnouncedEventKey = "";
let firstStatusLoad = true;

function setState(text) {
  ui.assistantState.textContent = text;
}

function stopSpeaking() {
  if ("speechSynthesis" in window) {
    speechSynthesis.cancel();
  }
  ui.robot.classList.remove("talking");
}

function chooseEnglishVoice() {
  const voices = speechSynthesis.getVoices();
  return (
    voices.find(v => v.lang === "en-US" && /female|zira|samantha|google/i.test(v.name)) ||
    voices.find(v => v.lang.startsWith("en")) ||
    voices[0]
  );
}

function speak(text) {
  ui.assistantMessage.textContent = text;

  if (!("speechSynthesis" in window)) {
    setState("Voice output unsupported");
    return;
  }

  stopSpeaking();

  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "en-US";
  utterance.rate = 0.95;
  utterance.pitch = 1.05;
  utterance.volume = 1;

  const voice = chooseEnglishVoice();
  if (voice) utterance.voice = voice;

  utterance.onstart = () => {
    ui.robot.classList.add("talking");
    setState("Speaking");
  };

  utterance.onend = () => {
    ui.robot.classList.remove("talking");
    setState("Ready");
  };

  utterance.onerror = event => {
    ui.robot.classList.remove("talking");
    setState("Voice output error");
    console.error("Speech synthesis error:", event);
  };

  speechSynthesis.speak(utterance);
}

if ("speechSynthesis" in window) {
  speechSynthesis.getVoices();
  speechSynthesis.onvoiceschanged = () => speechSynthesis.getVoices();
}

async function askZenva(question) {
  const clean = (question || "").trim();
  if (!clean) return;

  ui.questionInput.value = clean;
  ui.liveTranscript.textContent = clean;
  setState("Thinking");
  ui.assistantMessage.textContent = "Reading live Firebase data…";
  ui.askButton.disabled = true;

  try {
    const response = await fetch("/api/ask", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({question: clean}),
    });

    const data = await response.json();
    if (!response.ok) throw new Error(data.answer || "Request failed");

    speak(data.answer);
    await refreshStatus();
  } catch (error) {
    speak(`I could not complete the request. ${error.message}`);
  } finally {
    ui.askButton.disabled = false;
  }
}

function setupRecognition() {
  const Recognition =
    window.SpeechRecognition || window.webkitSpeechRecognition;

  if (!Recognition) {
    ui.micText.textContent = "Voice not supported";
    ui.assistantMessage.textContent =
      "Use the latest Google Chrome or Microsoft Edge. Firefox does not support browser speech recognition.";
    return;
  }

  recognition = new Recognition();
  recognition.lang = "en-US";
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;

  recognition.onstart = () => {
    isListening = true;
    stopSpeaking();
    ui.micButton.classList.add("listening");
    ui.micText.textContent = "Listening… click to stop";
    ui.liveTranscript.textContent = "Speak now…";
    setState("Listening");
  };

  recognition.onresult = event => {
    let interim = "";
    let finalText = "";

    for (let i = event.resultIndex; i < event.results.length; i++) {
      const text = event.results[i][0].transcript;
      if (event.results[i].isFinal) finalText += text;
      else interim += text;
    }

    ui.liveTranscript.textContent = finalText || interim || "Listening…";

    if (finalText.trim()) {
      ui.questionInput.value = finalText.trim();
    }
  };

  recognition.onspeechend = () => recognition.stop();

  recognition.onend = () => {
    isListening = false;
    ui.micButton.classList.remove("listening");
    ui.micText.textContent = "Start speaking";

    const transcript = ui.questionInput.value.trim();
    if (transcript) askZenva(transcript);
    else setState("Ready");
  };

  recognition.onerror = event => {
    isListening = false;
    ui.micButton.classList.remove("listening");
    ui.micText.textContent = "Start speaking";

    const messages = {
      "not-allowed":
        "Microphone permission was blocked. Click the lock icon beside the browser address, allow Microphone, and reload.",
      "service-not-allowed":
        "Browser speech service is blocked. Use Chrome or Edge and check internet access.",
      "no-speech":
        "I did not hear speech. Press the microphone and speak closer to it.",
      "audio-capture":
        "No microphone was found. Check Windows microphone settings.",
      "network":
        "Voice recognition needs internet access. Please check your connection.",
      "aborted":
        "Listening stopped.",
    };

    const message = messages[event.error] || `Microphone error: ${event.error}`;
    ui.assistantMessage.textContent = message;
    ui.liveTranscript.textContent = message;
    setState("Microphone error");
  };
}

ui.micButton.addEventListener("click", async () => {
  if (!recognition) return;

  if (isListening) {
    recognition.stop();
    return;
  }

  ui.questionInput.value = "";
  ui.liveTranscript.textContent = "Requesting microphone permission…";

  try {
    // Explicitly request permission first. This fixes many silent microphone failures.
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    stream.getTracks().forEach(track => track.stop());
    recognition.start();
  } catch (error) {
    ui.assistantMessage.textContent =
      "Microphone permission is blocked. Allow microphone access in the browser and Windows Privacy settings.";
    ui.liveTranscript.textContent = error.message;
    setState("Microphone blocked");
  }
});

ui.askButton.addEventListener("click", () => askZenva(ui.questionInput.value));
ui.questionInput.addEventListener("keydown", event => {
  if (event.key === "Enter") askZenva(ui.questionInput.value);
});

document.querySelectorAll("[data-question]").forEach(button => {
  button.addEventListener("click", () => askZenva(button.dataset.question));
});

function renderStatus(data) {
  ui.connectionDot.className = data.online ? "online" : "offline";
  ui.connectionText.textContent =
    data.online ? "Firebase connected" : "Firebase unavailable";

  ui.available.textContent = data.slot_counts.available;
  ui.occupied.textContent = data.slot_counts.occupied;
  ui.reserved.textContent = data.slot_counts.reserved;
  ui.inside.textContent = data.bookings.inside;

  ui.slots.innerHTML = Object.entries(data.slots)
    .map(([name, status]) => `
      <div class="slot ${status}">
        <strong>${name}</strong>
        <span>${status}</span>
      </div>
    `).join("");

  ui.temperature.textContent =
    data.sensors.temperature == null ? "—" : `${data.sensors.temperature} °C`;
  ui.humidity.textContent =
    data.sensors.humidity == null ? "—" : `${data.sensors.humidity} %`;
  ui.gas.textContent = data.sensors.gasStatus ?? "—";
  ui.relay.textContent = data.sensors.relay ?? "—";

  const gateOpen = data.gate.open === true;
  ui.gate.classList.toggle("open", gateOpen);
  ui.gateStatus.textContent =
    data.gate.open == null ? "Gate unknown" : gateOpen ? "Gate open" : "Gate closed";
  ui.gateCommand.textContent = `Command: ${data.gate.command ?? "unknown"}`;

  ui.eventPlate.textContent = data.event.plate ?? "—";
  ui.eventStatus.textContent = data.event.status ?? "—";
  ui.eventSlot.textContent = data.event.slot ?? "—";
  ui.updated.textContent = `Updated ${new Date().toLocaleTimeString()}`;

  // Announce only future booking changes, not the first page load.
  const key = [
    data.event.plate,
    data.event.status,
    data.event.entered,
    data.event.paid,
    data.event.fee,
    data.event.time
  ].join("|");

  if (firstStatusLoad) {
    lastAnnouncedEventKey = key;
    firstStatusLoad = false;
  } else if (key !== lastAnnouncedEventKey && data.event.plate !== "—") {
    lastAnnouncedEventKey = key;

    if (data.event.paid || data.event.status === "paid") {
      let message = `Payment completed for vehicle ${data.event.plate}.`;
      if (data.event.fee != null) message += ` The fee is ${data.event.fee}.`;
      message += " Thank you.";
      speak(message);
    } else if (data.event.entered) {
      speak(
        `Welcome. Vehicle ${data.event.plate} detected. ` +
        `Your assigned parking slot is ${data.event.slot}. Please proceed.`
      );
    } else if (["booked", "reserved", "active"].includes(data.event.status)) {
      speak(
        `Booking found for vehicle ${data.event.plate}. ` +
        `Assigned slot is ${data.event.slot}.`
      );
    }
  }
}

async function refreshStatus() {
  try {
    const response = await fetch("/api/status", {cache: "no-store"});
    if (!response.ok) throw new Error("Status request failed");
    renderStatus(await response.json());
  } catch (error) {
    ui.connectionDot.className = "offline";
    ui.connectionText.textContent = "Cannot reach Python server";
    console.error(error);
  }
}

setupRecognition();
refreshStatus();
setInterval(refreshStatus, 2500);

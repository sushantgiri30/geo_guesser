"use strict";

/* ------------------------------------------------------------------ */
/* The browser only RENDERS and animates. All logic — round selection,  */
/* street-view spawns, distance, scoring — lives in the Python server.  */
/* ------------------------------------------------------------------ */

const state = {
  sessionId: null,
  mode: "hint", // "street" (Mapillary) or "hint" (landmarks)
  totalRounds: 5,
  roundTime: 45,
  maxScore: 5000,
  perfectRadius: 50,
  mapillaryToken: null,
  currentRound: 0,
  totalScore: 0,
  roundScores: [],
  roundDetails: [], // { points, distance } per round
  guessMarker: null,
  revealLayer: null,
  viewer: null,
  timerInterval: null,
  secondsLeft: 45,
  revealed: false,
};

const $ = (id) => document.getElementById(id);

const screen = {
  start: $("start-screen"),
  game: $("game-screen"),
  end: $("end-screen"),
};

function showScreen(name) {
  Object.values(screen).forEach((el) => el.classList.remove("active"));
  screen[name].classList.add("active");
}

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Request failed");
  return data;
}

function formatDistance(meters) {
  if (meters < 1000) return `${Math.round(meters)} m`;
  return `${(meters / 1000).toFixed(meters < 10000 ? 1 : 0)} km`;
}

/* ------------------------------------------------------------------ */
/* Map — colorful Voyager base + free satellite layer toggle           */
/* ------------------------------------------------------------------ */

let map;
let satelliteActive = false;

const voyageLayer = L.tileLayer(
  "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
  { maxZoom: 19, subdomains: "abcd" }
);

const satLayer = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 19 }
);

function initMap() {
  map = L.map("map", {
    zoomControl: true,
    attributionControl: false,
  }).setView([27.7085, 85.322], 13);

  voyageLayer.addTo(map);

  map.on("click", (e) => {
    if (state.revealed) return;
    setGuess(e.latlng);
  });

  // Minimap grows on hover; Leaflet must know about the new size.
  const shell = $("map-shell");
  const expandBtn = $("map-expand-btn");

  function setExpanded(expanded) {
    shell.classList.toggle("map-expanded", expanded);
  }

  shell.addEventListener("mouseenter", () => setExpanded(true));
  shell.addEventListener("mouseleave", () => setExpanded(false));
  expandBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    setExpanded(!shell.classList.contains("map-expanded"));
  });
  shell.addEventListener("transitionend", () => map.invalidateSize());
}

function toggleLayers() {
  if (satelliteActive) {
    map.removeLayer(satLayer);
    map.addLayer(voyageLayer);
    $("layer-ico").textContent = "🛰️";
  } else {
    map.removeLayer(voyageLayer);
    map.addLayer(satLayer);
    $("layer-ico").textContent = "🗺️";
  }
  satelliteActive = !satelliteActive;
}

function setGuess(latlng) {
  if (state.guessMarker) {
    state.guessMarker.setLatLng(latlng);
  } else {
    state.guessMarker = L.marker(latlng, { draggable: true }).addTo(map);
    state.guessMarker.on("dragend", () => {
      if (state.revealed) return;
      $("submit-btn").disabled = false;
    });
  }
  $("submit-btn").disabled = false;
}

/* ------------------------------------------------------------------ */
/* Street view — MapillaryJS (free, needs a token from config.py)      */
/* ------------------------------------------------------------------ */

async function showStreetImage(imageId) {
  const container = $("street-view");
  $("street-note").classList.add("hidden");

  if (!state.mapillaryToken) {
    $("street-note").classList.remove("hidden");
    return;
  }

  if (!state.viewer) {
    state.viewer = new mapillary.Viewer({
      accessToken: state.mapillaryToken,
      container,
    });
  }

  try {
    await state.viewer.moveTo(imageId);
  } catch (err) {
    console.error("Mapillary move failed:", err);
    $("street-note").classList.remove("hidden");
    $("street-note").querySelector("p").textContent = "Couldn't load that street image.";
    $("street-note").querySelector(".note-sub").classList.add("hidden");
  }
}

/* ------------------------------------------------------------------ */
/* Timer                                                               */
/* ------------------------------------------------------------------ */

const ring = $("ring-progress");
const timerLabel = $("timer-label");

function startTimer() {
  state.secondsLeft = state.roundTime;
  updateTimerUI();

  state.timerInterval = setInterval(() => {
    state.secondsLeft -= 1;
    updateTimerUI();
    if (state.secondsLeft <= 0) {
      clearInterval(state.timerInterval);
      state.timerInterval = null;
      timeUp();
    }
  }, 1000);
}

function updateTimerUI() {
  timerLabel.textContent = state.secondsLeft;
  const pct = state.secondsLeft / state.roundTime;
  ring.style.strokeDashoffset = 100 - pct * 100;

  if (state.secondsLeft <= 10) {
    ring.style.stroke = "var(--danger)";
  } else if (state.secondsLeft <= 20) {
    ring.style.stroke = "#fbbf24";
  } else {
    ring.style.stroke = "var(--accent)";
  }
}

function stopTimer() {
  if (state.timerInterval) {
    clearInterval(state.timerInterval);
    state.timerInterval = null;
  }
}

function timeUp() {
  if (state.revealed) return;
  if (!state.guessMarker) setGuess(map.getCenter());
  submitGuess(true);
}

/* ------------------------------------------------------------------ */
/* Round lifecycle                                                     */
/* ------------------------------------------------------------------ */

async function startGame() {
  try {
    const config = await (await fetch("/api/config")).json();
    state.mapillaryToken = config.mapillary_token;

    const game = await api("/api/game/start", {});
    state.sessionId = game.session_id;
    state.mode = game.mode;
    state.totalRounds = game.total_rounds;
    state.roundTime = game.round_time;
    state.maxScore = game.max_score;
    state.perfectRadius = game.perfect_radius;
    state.currentRound = 0;
    state.totalScore = 0;
    state.roundScores = [];
    state.roundDetails = [];

    $("mode-note").textContent =
      state.mode === "street"
        ? "Street view enabled — you'll be dropped into random Kathmandu streets."
        : "No street view token set — playing with hints. Add one in config.py for street view.";

    showScreen("game");
    map.invalidateSize();
    await startRound();
  } catch (err) {
    alert("Could not start the game: " + err.message);
  }
}

async function startRound() {
  state.revealed = false;

  $("round-label").textContent = state.currentRound + 1;
  $("score-label").textContent = state.totalScore;
  $("hint-card").style.opacity = "1";
  $("submit-btn").textContent = "Confirm Guess";
  $("submit-btn").disabled = true;

  if (state.guessMarker) {
    map.removeLayer(state.guessMarker);
    state.guessMarker = null;
  }
  if (state.revealLayer) {
    state.revealLayer.remove();
    state.revealLayer = null;
  }
  $("reveal-overlay").classList.remove("show");
  hideResultOverlay();
  map.setView([27.7085, 85.322], 13);

  let spawn;
  try {
    spawn = await api("/api/round/spawn", {
      session_id: state.sessionId,
      round: state.currentRound,
    });
  } catch (err) {
    alert("Could not load round: " + err.message);
    return;
  }

  if (spawn.mode === "street") {
    $("street-label").textContent = "📍 Street View";
    $("hint-text").textContent =
      spawn.hint || "Look around — which part of Kathmandu is this?";
    await showStreetImage(spawn.image_id);
  } else {
    $("street-label").textContent = "💡 Hint Mode";
    $("hint-text").textContent = spawn.hint;
    $("street-note").classList.remove("hidden");
  }

  startTimer();
}

async function submitGuess(wasTimeout) {
  if (state.revealed) return;
  state.revealed = true;
  stopTimer();

  const guess = state.guessMarker.getLatLng();

  let resp;
  try {
    resp = await api("/api/guess", {
      session_id: state.sessionId,
      round: state.currentRound,
      lat: guess.lat,
      lng: guess.lng,
    });
  } catch (err) {
    alert("Could not submit your guess: " + err.message);
    state.revealed = false;
    startTimer();
    return;
  }

  state.roundScores.push(resp.points);
  state.roundDetails.push({ points: resp.points, distance: resp.distance });
  state.totalScore = resp.total_score;

  // Draw the actual spot + the line to your guess
  state.revealLayer = L.layerGroup().addTo(map);
  L.marker([resp.lat, resp.lng], {
    icon: L.divIcon({
      className: "place-pin",
      html: `<div class="pin-wrap">${resp.emoji || "📍"}</div>`,
      iconSize: [40, 40],
      iconAnchor: [20, 20],
    }),
  }).addTo(state.revealLayer);

  L.polyline(
    [[guess.lat, guess.lng], [resp.lat, resp.lng]],
    { color: "#f43f5e", weight: 2.5, dashArray: "6 6", opacity: 0.9 }
  ).addTo(state.revealLayer);

  map.flyTo([resp.lat, resp.lng], 15, { duration: 1.6 });

  // Fill the reveal panel
  const isStreet = resp.mode === "street";

  $("place-emoji").textContent = isStreet ? "📍" : resp.emoji;
  $("place-name").textContent = isStreet
    ? `${resp.lat.toFixed(4)}° N, ${resp.lng.toFixed(4)}° E`
    : resp.name;
  $("distance-label").textContent = `${formatDistance(resp.distance)} away`;
  $("points-earned").textContent = wasTimeout
    ? `+${resp.points} (time up!)`
    : `+${resp.points}`;
  $("stat-guess").textContent = `${guess.lat.toFixed(4)}, ${guess.lng.toFixed(4)}`;
  $("stat-actual").textContent = `${resp.lat.toFixed(4)}, ${resp.lng.toFixed(4)}`;

  // Only hint at a landmark when the spot is genuinely near one.
  if (isStreet && resp.nearest_landmark.distance <= 1000) {
    $("distance-label").textContent += ` · Near ${resp.nearest_landmark.name}`;
  }

  const isFinal = state.currentRound + 1 === state.totalRounds;
  $("next-btn").textContent = isFinal ? "See Results" : "Next Round";

  showResultMap(guess, { lat: resp.lat, lng: resp.lng }, resp.distance, resp.points);
}

/* ------------------------------------------------------------------ */
/* Full-screen guess-vs-actual comparison (5 seconds)                  */
/* ------------------------------------------------------------------ */

let resultMap = null;
let resultGuessMarker = null;
let resultActualMarker = null;
let resultLine = null;
let resultTimer = null;

function getResultMap() {
  if (resultMap) return resultMap;
  resultMap = L.map("result-map", {
    zoomControl: true,
    attributionControl: false,
  });
  L.tileLayer(
    "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
    { maxZoom: 19, subdomains: "abcd" }
  ).addTo(resultMap);
  return resultMap;
}

function showResultMap(guess, actual, distance, points) {
  const rm = getResultMap();

  if (resultGuessMarker) rm.removeLayer(resultGuessMarker);
  if (resultActualMarker) rm.removeLayer(resultActualMarker);
  if (resultLine) rm.removeLayer(resultLine);

  resultGuessMarker = L.marker([guess.lat, guess.lng], {
    icon: L.divIcon({
      className: "place-pin",
      html: '<div class="pin-wrap guess-pin">📍</div>',
      iconSize: [40, 40],
      iconAnchor: [20, 20],
    }),
  }).addTo(rm);

  resultActualMarker = L.marker([actual.lat, actual.lng], {
    icon: L.divIcon({
      className: "place-pin",
      html: '<div class="pin-wrap">🎯</div>',
      iconSize: [40, 40],
      iconAnchor: [20, 20],
    }),
  }).addTo(rm);

  resultLine = L.polyline(
    [[guess.lat, guess.lng], [actual.lat, actual.lng]],
    { color: "#f43f5e", weight: 3, dashArray: "7 7", opacity: 0.95 }
  ).addTo(rm);

  rm.fitBounds(
    [
      [guess.lat, guess.lng],
      [actual.lat, actual.lng],
    ],
    { padding: [60, 60], maxZoom: 15 }
  );

  $("result-points").textContent = `+${points}`;
  $("result-distance").textContent = formatDistance(distance);

  const isFinal = state.currentRound + 1 === state.totalRounds;
  $("result-next-btn").textContent = isFinal ? "See Results" : "Next Round";
  $("result-overlay").classList.add("show");

  clearTimeout(resultTimer);
  resultTimer = setTimeout(() => {
    $("result-overlay").classList.remove("show");
    $("reveal-overlay").classList.add("show");
  }, 5000);
}

function skipResult() {
  hideResultOverlay();
  nextRound();
}

function hideResultOverlay() {
  clearTimeout(resultTimer);
  resultTimer = null;
  $("result-overlay").classList.remove("show");
}

function nextRound() {
  if (state.currentRound + 1 >= state.totalRounds) {
    showEndScreen();
  } else {
    state.currentRound += 1;
    startRound();
  }
}

/* ------------------------------------------------------------------ */
/* End screen                                                          */
/* ------------------------------------------------------------------ */

function showEndScreen() {
  $("reveal-overlay").classList.remove("show");

  const pct = state.totalScore / (state.totalRounds * state.maxScore);
  let trophy, rating;

  if (pct >= 0.9) {
    trophy = "🏆";
    rating = "Kathmandu Master";
  } else if (pct >= 0.7) {
    trophy = "🌄";
    rating = "Valley Explorer";
  } else if (pct >= 0.5) {
    trophy = "🧭";
    rating = "Street Sherlock";
  } else if (pct >= 0.3) {
    trophy = "🗺️";
    rating = "Casual Wanderer";
  } else {
    trophy = "🥾";
    rating = "Lost in the Valley";
  }

  $("end-trophy").textContent = trophy;
  $("end-rating").textContent = rating;
  $("final-score").textContent = state.totalScore.toLocaleString();

  setTimeout(() => {
    $("score-bar-fill").style.width = `${pct * 100}%`;
  }, 150);

  // Stats: total / best / average
  const details = state.roundDetails;
  const totalDist = details.reduce((sum, d) => sum + d.distance, 0);
  const best = details.length
    ? details.reduce((m, d) => (d.points > m.points ? d : m))
    : { points: 0 };
  const avgDist = details.length ? totalDist / details.length : 0;

  $("stat-total-dist").textContent = formatDistance(totalDist);
  $("stat-best").textContent = `${best.points.toLocaleString()} pts`;
  $("stat-avg-dist").textContent = formatDistance(avgDist);

  // Per-round breakdown
  const breakdown = $("round-breakdown");
  breakdown.innerHTML = "";
  details.forEach((d, i) => {
    const item = document.createElement("div");
    item.className = "breakdown-item";
    if (d.points >= 4900) {
      item.classList.add("perfect");
    } else if (d.points >= 3500) {
      item.classList.add("good");
    } else if (d.points >= 2000) {
      item.classList.add("mid");
    } else {
      item.classList.add("bad");
    }
    item.innerHTML = `
      <div class="br-rank">${d.points >= 4900 ? "🎯" : `R${i + 1}`}</div>
      <div class="br-score">${d.points}</div>
      <div class="br-dist">${formatDistance(d.distance)}</div>`;
    breakdown.appendChild(item);
  });

  showScreen("end");
}

/* ------------------------------------------------------------------ */
/* Events                                                              */
/* ------------------------------------------------------------------ */

$("start-btn").addEventListener("click", startGame);
$("submit-btn").addEventListener("click", () => submitGuess(false));
$("next-btn").addEventListener("click", nextRound);
$("result-next-btn").addEventListener("click", skipResult);
$("replay-btn").addEventListener("click", startGame);
$("layer-toggle").addEventListener("click", toggleLayers);

initMap();
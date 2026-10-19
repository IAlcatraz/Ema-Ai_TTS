import * as THREE from 'three';

import { VRM_PATH, WS_URL, TRANSPARENT_MODE, RENDER, CHARACTER } from './config.js';
import { createScene, applyFilmSettings } from './scene/sceneSetup.js';
import { getScenePreset } from './scene/scenePresets.js';
import { loadSceneObjects, describeObjects } from './scene/objectLoader.js';
import { SCENE_OBJECTS } from './scene/objectsConfig.js';
import { loadRoom, fitRoom } from './scene/roomLoader.js';
import { ROOM_CONFIG } from './scene/roomConfig.js';
import { createLocationRegistry } from './scene/locationRegistry.js';
import { NAMED_LOCATIONS, DEBUG_MARKERS } from './scene/locationsConfig.js';
import { loadVRM } from './core/vrmLoader.js';
import { MToonTuner } from './core/mtoonTuner.js';
import { Bokeh } from './core/bokeh.js';
import { AudioManager } from './core/audioManager.js';
import { PlaybackController } from './core/playbackController.js';
import { AnimationManager } from './animation/animationManager.js';
import { MovementController } from './animation/movementController.js';
import { showSubtitleStreaming, setSubtitlesEnabled, getSubtitlesEnabled, clearSubtitles } from './overlay/subtitles.js';
import { initFpsMeter } from './overlay/fpsMeter.js';
import { initWebcam, takePictureAndUpload } from './interaction/webcam.js';
import { connectWS, initUI } from './ui.js';
import { initVRMClickDetector } from './interaction/vrmClickDetector.js';
import { isVRSupported } from './vr/vrManager.js';
import { MODEL_CONFIGS } from './characters/riko/models_config.js';

// --- WebSocket + UI (early init, no dependencies) ---
function handleServerMessage(msg) {
  console.log("📩 Received from server:", msg);
}
initUI();

// Inside the Electron desktop shell the page background goes transparent so
// only the avatar is drawn over the desktop — see TRANSPARENT_MODE in config.js.
if (TRANSPARENT_MODE) document.body.classList.add('transparent');

// --- Subtitle toggle ---
const subtitleBtn = document.getElementById('toggle-subtitles-button');
if (subtitleBtn) {
  subtitleBtn.addEventListener('click', () => {
    const nowEnabled = !getSubtitlesEnabled();
    setSubtitlesEnabled(nowEnabled);
    subtitleBtn.classList.toggle('off', !nowEnabled);
  });
}

// --- App State ---
let vrm = null;
let renderer = null;
let scene = null;
let camera = null;
let controls = null;
let audioMgr = null;
let animationMgr = null;
let movementController = null;
let playbackController = null;
let vrManager = null;
let vrPositionTracker = null;
let bokeh = null;
const clock = new THREE.Clock();

(async () => {
  // --- Check VR support ---
  const vrSupported = await isVRSupported();
  if (vrSupported) console.log('WebXR VR supported - VR mode available');

  // --- Scene preset ---
  const preset = getScenePreset(CHARACTER);

  // --- Scene Setup ---
  ({ renderer, scene, camera, controls } = createScene({
    ...(preset ? {
      lights: preset.lights,
      fov: preset.camera.fov,
      cameraPosition: preset.camera.position,
      controlsTarget: preset.camera.target,
    } : {}),
  }));
  if (preset) applyFilmSettings(renderer, scene, preset.renderer);

  // --- Room ---
  if (preset?.room?.url) {
    const room = await loadRoom(scene, {
      enabled: true,
      url: preset.room.url,
      position: [0, 0, 0], rotation: [0, 0, 0], scale: [1, 1, 1],
      fixDepth: preset.room.fixDepth,
    });
    fitRoom(room, preset.room);
  } else {
    await loadRoom(scene, ROOM_CONFIG);
  }

  // --- Scene Objects ---
  const sceneObjects = await loadSceneObjects(scene, SCENE_OBJECTS);
  window.sceneObjects = sceneObjects;
  window.describeSceneObjects = () => describeObjects(sceneObjects);

  // --- Named locations ---
  const locations = createLocationRegistry({
    scene,
    namedLocations: NAMED_LOCATIONS,
    sceneObjects,
    debugMarkers: DEBUG_MARKERS,
  });
  window.locations = locations;

  // --- Load VRM ---
  const loadActiveVRM = async (modelName = 'Riko1') => {
    console.log(`[app] Loading model: ${modelName}`);
    const config = MODEL_CONFIGS[modelName] || MODEL_CONFIGS['Riko1'];

    try {
      const vrmData = await loadVRM(config.path, scene, config.rotationY);
      if (vrm) scene.remove(vrm.scene);
      vrm = vrmData.vrm;

      if (preset) {
        if (preset.mtoon) new MToonTuner(vrm).apply(preset.mtoon);
        if (preset.avatar) {
          vrm.scene.position.set(...preset.avatar.position);
          vrm.scene.rotation.y = preset.avatar.rotationY ?? config.rotationY;
        }
        vrm.scene.traverse((o) => { if (o.isMesh) o.receiveShadow = true; });
      }

      // Update managers
      if (audioMgr) audioMgr.vrm = vrm;
      if (animationMgr) {
        animationMgr.updateVRM(vrm);
        animationMgr.setState('idle');
        animationMgr.update(0);
      }
      if (movementController) {
        movementController.updateVRM(vrm);
        movementController.resetCache();
        await movementController.loadIdleAnimation('./animations/mixamo/Idle.fbx');
        await movementController.loadWalkAnimation('./animations/mixamo/Walking_inplace.fbx');
        await movementController.loadAdditiveAnimation('./animations/mixamo/Waving.fbx', 'wave');
        if (movementController.mixer && movementController.actions.idle) {
          movementController.actions.idle.setEffectiveWeight(1.0);
          movementController.actions.idle.play();
          movementController.mixer.update(0);
        }
      }

      initVRMClickDetector(renderer, camera, vrm, (region, partName) => {
        if (!audioMgr || !movementController || animationMgr?.isPlaying) return;
        const [expr, gestures] = POKE_REACTIONS[region] || POKE_REACTIONS.body;
        audioMgr.setExpression(expr);
        const gesture = gestures[Math.floor(Math.random() * gestures.length)];
        movementController.playMixamo(
          `./animations/mixamo/Gestures%20Pack%20Basic/${g(gesture)}.fbx`,
          { play_once: true, track_position: false },
        );
        clearTimeout(pokeReactionTimeout);
        pokeReactionTimeout = setTimeout(() => {
          if (!animationMgr?.isPlaying) audioMgr.setExpression('relaxed');
        }, 1500);
      });
      console.log(`[app] Model ${modelName} loaded successfully`);
    } catch (err) {
      console.error(`[app] Failed to load model ${modelName}:`, err);
    }
  };

  window.addEventListener('change-vrm-model', async (e) => {
    await loadActiveVRM(e.detail.modelName);
  });

  await loadActiveVRM('Riko1');

  bokeh = new Bokeh({ renderer, scene, camera, controls });
  if (preset?.bokeh) bokeh.apply(preset.bokeh);
  window.addEventListener('resize', () => bokeh.setSize(window.innerWidth, window.innerHeight));

  let pokeReactionTimeout = null;
  const g = (n) => n.replace(/ /g, '%20');
  const POKE_REACTIONS = {
    head: ['happy', ['happy hand gesture', 'acknowledging']],
    hair: ['relaxed', ['happy hand gesture', 'weight shift']],
    face: ['surprised', ['look away gesture', 'weight shift']],
    left_cat_ear: ['surprised', ['look away gesture', 'annoyed head shake']],
    right_cat_ear: ['surprised', ['look away gesture', 'annoyed head shake']],
    tail: ['surprised', ['look away gesture', 'weight shift']],
    neck: ['surprised', ['weight shift', 'look away gesture']],
    left_shoulder: ['relaxed', ['acknowledging']],
    right_shoulder: ['relaxed', ['acknowledging']],
    left_arm: ['relaxed', ['acknowledging', 'weight shift']],
    right_arm: ['relaxed', ['acknowledging', 'weight shift']],
    left_hand: ['happy', ['happy hand gesture', 'acknowledging']],
    right_hand: ['happy', ['happy hand gesture', 'acknowledging']],
    belly: ['surprised', ['weight shift', 'annoyed head shake']],
    chest: ['surprised', ['look away gesture', 'weight shift', 'relieved sigh']],
    bust: ['surprised', ['look away gesture', 'weight shift', 'relieved sigh']],
    hips: ['surprised', ['look away gesture', 'weight shift']],
    left_thigh: ['surprised', ['annoyed head shake', 'shaking head no']],
    right_thigh: ['surprised', ['annoyed head shake', 'shaking head no']],
    left_shin: ['surprised', ['weight shift', 'look away gesture']],
    right_shin: ['surprised', ['weight shift', 'look away gesture']],
    left_foot: ['surprised', ['weight shift', 'look away gesture']],
    right_foot: ['surprised', ['weight shift', 'look away gesture']],
    clothing: ['surprised', ['annoyed head shake', 'weight shift']],
    body: ['surprised', ['acknowledging', 'weight shift']],
  };
  initVRMClickDetector(renderer, camera, vrm, (region, partName) => {
    if (!audioMgr || !movementController || animationMgr?.isPlaying) return;
    const [expr, gestures] = POKE_REACTIONS[region] || POKE_REACTIONS.body;
    audioMgr.setExpression(expr);
    const gesture = gestures[Math.floor(Math.random() * gestures.length)];
    movementController.playMixamo(
      `./animations/mixamo/Gestures%20Pack%20Basic/${g(gesture)}.fbx`,
      { play_once: true, track_position: false },
    );
    clearTimeout(pokeReactionTimeout);
    pokeReactionTimeout = setTimeout(() => {
      if (!animationMgr?.isPlaying) audioMgr.setExpression('relaxed');
    }, 1500);
  });

  if (vrSupported) {
    const { VRManager } = await import('./vr/vrManager.js');
    const { VRPositionTracker } = await import('./vr/vrPositionTracker.js');
    vrManager = new VRManager(renderer, scene, camera, controls);
    vrManager.init();
    vrManager.initTouchDetection(vrm, {
      onTouch: (region, boneName, handIndex) => { console.log(`VR touch: ${region} (hand ${handIndex})`); },
    });
    vrPositionTracker = new VRPositionTracker(camera, vrManager.getDolly());
    window.addEventListener('vr-session-start', () => vrPositionTracker.enable());
    window.addEventListener('vr-session-end', () => vrPositionTracker.disable());
    window.addEventListener('vr-unlock-audio', async () => {
      try { await playbackController?.unlockOnce(); } catch (e) {}
    });
    window.vrManager = vrManager;
    window.vrPositionTracker = vrPositionTracker;
  }

  audioMgr = new AudioManager(vrm);
  animationMgr = new AnimationManager(vrm, audioMgr, renderer, scene, camera, controls);
  animationMgr.setState('idle');
  movementController = new MovementController(vrm, scene, animationMgr);
  await movementController.loadIdleAnimation('./animations/mixamo/Idle.fbx');
  await movementController.loadWalkAnimation('./animations/mixamo/Walking_inplace.fbx');
  await movementController.loadAdditiveAnimation('./animations/mixamo/Waving.fbx', 'wave');

  playbackController = new PlaybackController(audioMgr);
  playbackController.initPersistent();
  window.playbackController = playbackController;
  window.audioMgr = audioMgr;
  window.animationMgr = animationMgr;

  const onFirstGesture = async () => {
    try {
      console.log('[Audio] First gesture detected, attempting to unlock audio...');
      await playbackController.unlockOnce();
      console.log('[Audio] Unlock attempt completed');
    } catch (e) {
      console.error('[Audio] Unlock failed:', e);
    }
    window.removeEventListener('pointerdown', onFirstGesture, { capture: true });
    window.removeEventListener('touchend', onFirstGesture, { capture: true });
    window.removeEventListener('keydown', onFirstGesture, { capture: true });
    window.removeEventListener('click', onFirstGesture, { capture: true });
  };
  window.addEventListener('pointerdown', onFirstGesture, { capture: true });
  window.addEventListener('touchend', onFirstGesture, { capture: true });
  window.addEventListener('keydown', onFirstGesture, { capture: true });
  window.addEventListener('click', onFirstGesture, { capture: true });

  clock.start();
  const fpsTick = initFpsMeter();
  function animate() {
    const deltaTime = clock.getDelta();
    fpsTick();
    if (movementController) movementController.update(deltaTime);
    if (animationMgr) animationMgr.update(deltaTime);
    if (vrManager) vrManager.update();
    if (vrPositionTracker) vrPositionTracker.update();
    vrm.update(deltaTime);
    if (vrManager && vrManager.isInVRSession) renderer.render(scene, camera);
    else bokeh.render();
    if (!vrManager || !vrManager.isInVRSession) controls.update();
  }

  if (vrSupported) {
    renderer.setAnimationLoop(animate);
  } else {
    const minFrameMs = RENDER.maxFps ? 1000 / RENDER.maxFps : 0;
    let lastFrameMs = -Infinity;
    (function loop(now = 0) {
      requestAnimationFrame(loop);
      if (now - lastFrameMs < minFrameMs) return;
      lastFrameMs = now;
      animate();
    })();
  }

  console.log('[app] avatar ready');

  let playbackChain = Promise.resolve();
  let queueDepth = 0;
  let playbackEpoch = 0;
  let interruptionReactionTimeout = null;
  function enqueuePlayback(task) {
    queueDepth += 1;
    playbackChain = playbackChain.then(task).catch((e) => console.error('[playback] queued clip failed:', e)).finally(() => { queueDepth -= 1; });
    return playbackChain;
  }

  const onAvatarMessage = async ({ data }) => {
    let msg;
    try { msg = JSON.parse(data); } catch { return; }

    // DEBUG: Log every message for visibility
    console.log("📩 Received from server:", msg);

    if (msg.type === 'walk_to') {

      const { x, y, z, speed } = msg;
      if (speed) movementController.setSpeed(speed);
      movementController.walkTo(x, y, z);
    }
    if (msg.type === 'stop_movement') movementController.stop();
    if (msg.type === 'teleport_to') {
      const { x, y, z } = msg;
      movementController.teleportTo(x, y, z);
    }
    if (msg.type === 'set_speed') movementController.setSpeed(msg.speed);
    if (msg.type === 'load_walk_animation') await movementController.loadWalkAnimation(msg.url);
    if (msg.type === 'load_idle_animation') await movementController.loadIdleAnimation(msg.url);
    if (msg.type === 'set_additive_weight') {
      const { anim_name, weight, duration } = msg;
      movementController.setAdditiveWeight(anim_name, weight, duration || 0.25);
    }
    if (msg.type === 'play_additive_once') {
      const { anim_name, fade_in, fade_out } = msg;
      movementController.playAdditiveOnce(anim_name, fade_in || 0.25, fade_out || 0.25);
    }
    if (msg.type === 'load_additive_animation') {
      const { url, name } = msg;
      await movementController.loadAdditiveAnimation(url, name);
    }
    if (msg.type === 'load_and_play_additive') {
      const { url, name, weight = 1.0, play_once = false, fade_in = 0.25, fade_out = 0.25 } = msg;
      try {
        const loaded = await movementController.loadAdditiveAnimation(url, name);
        if (loaded) {
          if (play_once) movementController.playAdditiveOnce(name, fade_in, fade_out);
          else movementController.setAdditiveWeight(name, weight, fade_in);
        }
      } catch (err) { console.error(`Failed to load and play additive animation ${name}:`, err); }
    }
    if (msg.type === 'set_state') {
      const { state } = msg;
      if (animationMgr && ['idle', 'listening', 'thinking', 'talking'].includes(state)) animationMgr.setState(state);
    }
    if (msg.type === 'stop_talk') {
      playbackEpoch += 1;
      playbackController.stop();
      clearSubtitles();
      if (interruptionReactionTimeout !== null) {
        clearTimeout(interruptionReactionTimeout);
        interruptionReactionTimeout = null;
      }
      audioMgr.setExpression('surprised');
      animationMgr.setState('idle');
      movementController.playMixamo('./animations/mixamo/Gestures%20Pack%20Basic/acknowledging.fbx', { play_once: true, track_position: false });
      interruptionReactionTimeout = setTimeout(() => {
        audioMgr.setExpression('relaxed');
        interruptionReactionTimeout = null;
      }, 1800);
      return;
    }
    if (msg.type === 'set_movement_lock_duration') {
      const { duration } = msg;
      if (animationMgr && typeof duration === 'number') animationMgr.setMovementLockDuration(duration);
    }
    if (msg.type === 'start_animation') {
      const { audio_path, expression = 'neutral', audio_text, audio_duraction } = msg;
      const queuedEpoch = playbackEpoch;
      if (interruptionReactionTimeout !== null) {
        clearTimeout(interruptionReactionTimeout);
        interruptionReactionTimeout = null;
      }
      enqueuePlayback(async () => {
        if (queuedEpoch !== playbackEpoch) return;
        const queuedAt = performance.now();
        audioMgr.setExpression(expression);
        try { await playbackController.unlockOnce(); } catch (e) {}

        // Store subtitles to be displayed when audio actually starts
        window.pendingSubtitles = { text: audio_text, duration: audio_duraction };

        try {
          const ok = await playbackController.playAudioUrl(audio_path);
          if (queuedEpoch !== playbackEpoch) return;
          if (!ok) {
            animationMgr.setState('idle');
            return;
          }
          await playbackController.waitUntilEnded(audio_duraction);
        } catch (err) {
          console.error('[playback] playback error:', err);
          animationMgr.setState('idle');
        }
      });
    }
    if (msg.type === 'play_sfx') {
      const { audio_path } = msg;
      const sfx = new Audio(audio_path);
      sfx.play().catch(() => {});
    }
    if (msg.type === 'start_vrma') {
      const { animation_url, play_once = false, crop_start = 0.0, crop_end = 0.0, lock_position = false, track_position = true } = msg;
      await movementController.playVRMA(animation_url, { play_once, crop_start, crop_end, lock_position, track_position });
    }
    if (msg.type === 'start_mixamo') {
      const { animation_url, play_once = false, lock_position = false, track_position = true } = msg;
      await movementController.playMixamo(animation_url, { play_once, lock_position, track_position });
    }
    if (msg.type === 'take_picture') await takePictureAndUpload();
  };

  let wsRetryMs = 1000;
  function connectAvatarWS() {
    const ws = new WebSocket(WS_URL);
    ws.onopen = () => { wsRetryMs = 1000; console.log('✅ WebSocket connected'); };
    ws.onerror = err => console.error('WS error', err);
    ws.onmessage = onAvatarMessage;
    ws.onclose = () => {
      console.warn(`[ws] avatar socket closed — reconnecting in ${wsRetryMs}ms`);
      setTimeout(connectAvatarWS, wsRetryMs);
      wsRetryMs = Math.min(wsRetryMs * 2, 10000);
    };
  }
  connectAvatarWS();

  window.addEventListener('audio-started', (e) => {
    const { url, startTime } = e.detail;
    if (animationMgr) animationMgr.play();
    if (window.pendingSubtitles) {
      const { text, duration } = window.pendingSubtitles;
      // The audio actually starts at startTime.
      // We trigger the subtitles immediately as that's the best we can do with the current API,
      // but the event itself is now precisely timed to the first PCM chunk.
      showSubtitleStreaming(text, duration);
      window.pendingSubtitles = null;
    }
  });

})();

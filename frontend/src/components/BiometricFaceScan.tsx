import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import * as faceapi from "face-api.js";
import type {
  BiometricScanPath,
  BiometricScanResult,
  BiometricScanSample,
  BiometricScanStepResult,
  BiometricScanTarget,
} from "../types";

interface Props {
  open: boolean;
  challengeId: string;
  onClose: () => void;
  onVerified: (result: BiometricScanResult) => void;
}

interface ScanStep {
  label: string;
  hint: string;
  target: BiometricScanTarget;
}

interface Pose {
  yaw: number;
  pitch: number;
  roll: number;
  faceCenterX: number;
  faceCenterY: number;
}

const REQUIRED_STABLE_FRAMES = 1;
const MODEL_URL = "/models";
const MAX_SAMPLE_COUNT = 90;
const STILL_WINDOW = 12;
const POSE_JUMP_WARN = 0.85;
const POSE_JUMP_STOP = 1.25;
const PROFILE_IMAGES = [
  "/face_profiles/u_an_front.jpg",
  "/face_profiles/u_an_left.jpg",
  "/face_profiles/u_an_right.jpg",
];
const FACE_MATCH_THRESHOLD = 0.48;

const stepsForPath = (path: BiometricScanPath): ScanStep[] => {
  const firstVertical = path === "clockwise" ? "phía trên" : "phía dưới";
  return [
    { label: "Bắt đầu", hint: "Nhìn thẳng vào giữa khung oval", target: "center" },
    { label: "Bắt đầu vòng", hint: "Quay đầu sang một bên bất kỳ", target: "sideA" },
    { label: "Tiếp tục vòng", hint: `Tiếp tục quay vòng qua ${firstVertical}`, target: "verticalA" },
    { label: "Nửa vòng", hint: "Tiếp tục quay qua bên còn lại", target: "sideB" },
    { label: "Về giữa", hint: "Quay mặt về giữa khung", target: "center" },
  ];
};

let modelPromise: Promise<void> | null = null;
let profilePromise: Promise<number[][]> | null = null;

const loadModels = () => {
  if (!modelPromise) {
    modelPromise = Promise.all([
      faceapi.nets.tinyFaceDetector.loadFromUri(MODEL_URL),
      faceapi.nets.faceLandmark68Net.loadFromUri(MODEL_URL),
      faceapi.nets.faceRecognitionNet.loadFromUri(MODEL_URL),
    ]).then(() => undefined);
  }
  return modelPromise;
};

const descriptorFromImage = async (src: string) => {
  const image = await faceapi.fetchImage(src);
  const result = await faceapi
    .detectSingleFace(image, new faceapi.TinyFaceDetectorOptions({ inputSize: 224, scoreThreshold: 0.45 }))
    .withFaceLandmarks()
    .withFaceDescriptor();
  return result ? Array.from(result.descriptor) : null;
};

const loadProfileDescriptors = async () => {
  await loadModels();
  if (!profilePromise) {
    profilePromise = Promise.all(PROFILE_IMAGES.map((src) => descriptorFromImage(src))).then((descriptors) =>
      descriptors.filter((descriptor): descriptor is number[] => Boolean(descriptor)),
    );
  }
  return profilePromise;
};

const descriptorFromVideo = async (video: HTMLVideoElement) => {
  const result = await faceapi
    .detectSingleFace(video, new faceapi.TinyFaceDetectorOptions({ inputSize: 224, scoreThreshold: 0.5 }))
    .withFaceLandmarks()
    .withFaceDescriptor();
  return result ? Array.from(result.descriptor) : null;
};

const faceDistance = (a: number[], b: number[]) => {
  if (a.length !== b.length || a.length === 0) return Number.POSITIVE_INFINITY;
  return Math.sqrt(a.reduce((sum, value, index) => sum + (value - b[index]) ** 2, 0));
};

const bestFaceDistance = (faceDescriptor: number[], profileDescriptors: number[][]) =>
  Math.min(...profileDescriptors.map((descriptor) => faceDistance(faceDescriptor, descriptor)));

const average = (points: faceapi.Point[]) => ({
  x: points.reduce((sum, point) => sum + point.x, 0) / points.length,
  y: points.reduce((sum, point) => sum + point.y, 0) / points.length,
});

const poseFromDetection = (
  detection: faceapi.WithFaceLandmarks<{ detection: faceapi.FaceDetection }, faceapi.FaceLandmarks68>,
  video: HTMLVideoElement,
): Pose => {
  const box = detection.detection.box;
  const landmarks = detection.landmarks;
  const nose = landmarks.getNose();
  const leftEye = average(landmarks.getLeftEye());
  const rightEye = average(landmarks.getRightEye());
  const noseTip = nose[3] ?? nose[Math.floor(nose.length / 2)];
  const centerX = box.x + box.width / 2;
  const centerY = box.y + box.height / 2;
  const videoWidth = video.videoWidth || 640;
  const videoHeight = video.videoHeight || 480;

  return {
    yaw: (noseTip.x - centerX) / box.width,
    pitch: (noseTip.y - centerY) / box.height,
    roll: (rightEye.y - leftEye.y) / Math.max(1, rightEye.x - leftEye.x),
    faceCenterX: (centerX - videoWidth / 2) / (videoWidth / 2),
    faceCenterY: (centerY - videoHeight / 2) / (videoHeight / 2),
  };
};

const targetOk = (target: BiometricScanTarget, pose: Pose, sideSign: number, path: BiometricScanPath) => {
  const verticalAOk = path === "clockwise" ? pose.pitch < -0.055 : pose.pitch > 0.055;
  switch (target) {
    case "center":
      return Math.abs(pose.yaw) < 0.2 && Math.abs(pose.pitch) < 0.2 && Math.abs(pose.roll) < 0.22;
    case "sideA":
      return Math.abs(pose.yaw) > 0.065;
    case "verticalA":
      return verticalAOk;
    case "sideB":
      return sideSign !== 0 && Math.sign(pose.yaw) === -sideSign && Math.abs(pose.yaw) > 0.065;
  }
};

const statusFor = (target: BiometricScanTarget, path: BiometricScanPath) => {
  if (target === "center") return "Giữ khuôn mặt thấy rõ trong khung và nhìn gần thẳng.";
  if (target === "sideA") return "Quay mặt sang một bên bất kỳ.";
  if (target === "verticalA") return path === "clockwise" ? "Tiếp tục quay vòng qua phía trên." : "Tiếp tục quay vòng qua phía dưới.";
  return "Tiếp tục quay sang bên còn lại.";
};

const frameSignature = (video: HTMLVideoElement) => {
  const canvas = document.createElement("canvas");
  canvas.width = 24;
  canvas.height = 18;
  const ctx = canvas.getContext("2d");
  if (!ctx) return 0;
  ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
  const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
  let hash = 2166136261;
  for (let i = 0; i < pixels.length; i += 16) {
    const lum = Math.round((pixels[i] + pixels[i + 1] + pixels[i + 2]) / 3);
    hash ^= lum;
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
};

const poseDistance = (a: Pose, b: Pose) =>
  Math.abs(a.yaw - b.yaw) + Math.abs(a.pitch - b.pitch) + Math.abs(a.roll - b.roll);

const continuousScanError = (samples: BiometricScanSample[], continuityBreaks: number) => {
  if (samples.length < 12) return "Cần quét chuyển động liên tục lâu hơn một chút.";
  if (continuityBreaks > 1) return "Phát hiện chuyển động bị nhảy khung. Hãy dùng mặt thật và quay liên tục.";

  let totalMotion = 0;
  let maxPoseJump = 0;
  let lowScoreSamples = 0;
  const signatures = new Set<number>();

  for (let index = 0; index < samples.length; index += 1) {
    const sample = samples[index];
    signatures.add(sample.frameSignature);
    if (sample.detectionScore < 0.5) lowScoreSamples += 1;

    if (index > 0) {
      const previous = samples[index - 1];
      const gap = sample.elapsedMs - previous.elapsedMs;
      if (gap <= 0) return "Chuỗi frame sinh trắc học không hợp lệ.";
      const jump = poseDistance(sample.pose, previous.pose);
      totalMotion += jump;
      maxPoseJump = Math.max(maxPoseJump, jump);
    }
  }

  if (lowScoreSamples > 2) return "Một số frame khuôn mặt chưa đủ rõ, vui lòng quét lại.";
  if (signatures.size < 7) return "Chuỗi hình ảnh quá giống ảnh tĩnh, vui lòng quay mặt thật liên tục.";
  if (maxPoseJump > 1) return "Phát hiện pose nhảy quá nhanh, nghi ngờ đổi ảnh theo từng hướng.";
  if (totalMotion < 0.55) return "Chuyển động khuôn mặt chưa đủ liên tục để xác minh.";
  return "";
};

const earlyStaticError = (samples: BiometricScanSample[]) => {
  if (samples.length < STILL_WINDOW) return "";
  const windowSamples = samples.slice(-STILL_WINDOW);
  const signatures = new Set(windowSamples.map((sample) => sample.frameSignature));
  let motion = 0;
  for (let index = 1; index < windowSamples.length; index += 1) {
    motion += poseDistance(windowSamples[index].pose, windowSamples[index - 1].pose);
  }
  if (signatures.size <= 3 && motion < 0.035) {
    return "Khung hình gần như ảnh tĩnh. Hãy dùng mặt thật và quay liên tục trước camera.";
  }
  return "";
};

export const BiometricFaceScan = ({ open, challengeId, onClose, onVerified }: Props) => {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const timerRef = useRef<number | null>(null);
  const stableFramesRef = useRef(0);
  const checkpointRef = useRef(0);
  const isDetectingRef = useRef(false);
  const startedAtRef = useRef(new Date().toISOString());
  const stepResultsRef = useRef<BiometricScanStepResult[]>([]);
  const sampleResultsRef = useRef<BiometricScanSample[]>([]);
  const lastPoseRef = useRef<Pose | null>(null);
  const continuityBreaksRef = useRef(0);
  const identityCheckedRef = useRef(false);
  const identityCheckingRef = useRef(false);
  const sideSignRef = useRef(0);
  const pathRef = useRef<BiometricScanPath>("clockwise");

  const [checkpoint, setCheckpoint] = useState(0);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);
  const [retryNonce, setRetryNonce] = useState(0);
  const [liveStatus, setLiveStatus] = useState("Đang tải mô hình nhận diện...");
  const [scanResult, setScanResult] = useState<BiometricScanResult | null>(null);

  const stopCamera = () => {
    if (timerRef.current) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    }
  };

  const verifyCurrentIdentity = async (video: HTMLVideoElement) => {
    identityCheckingRef.current = true;
    setLiveStatus("Đang đối chiếu khuôn mặt với hồ sơ tài khoản...");
    try {
      const [faceDescriptor, profileDescriptors] = await Promise.all([
        descriptorFromVideo(video),
        loadProfileDescriptors(),
      ]);
      if (!faceDescriptor) {
        setError("Chưa lấy được đặc trưng khuôn mặt hiện tại, vui lòng quét lại.");
        stopCamera();
        return false;
      }
      if (profileDescriptors.length === 0) {
        setError("Chưa tải được hồ sơ sinh trắc học đã lưu của tài khoản.");
        stopCamera();
        return false;
      }
      if (bestFaceDistance(faceDescriptor, profileDescriptors) > FACE_MATCH_THRESHOLD) {
        setError("Khuôn mặt không khớp với hồ sơ sinh trắc học đã lưu của tài khoản.");
        stopCamera();
        return false;
      }
      identityCheckedRef.current = true;
      return true;
    } finally {
      identityCheckingRef.current = false;
    }
  };

  const scanCurrentStep = async () => {
    const video = videoRef.current;
    if (!video || video.readyState < 2 || isDetectingRef.current) return;

    isDetectingRef.current = true;
    try {
      const detections = await faceapi
        .detectAllFaces(video, new faceapi.TinyFaceDetectorOptions({ inputSize: 224, scoreThreshold: 0.5 }))
        .withFaceLandmarks();

      if (detections.length !== 1) {
        stableFramesRef.current = 0;
        setLiveStatus(
          detections.length > 1
            ? "Có nhiều khuôn mặt trong khung, chỉ để một người trước camera."
            : "Chưa thấy rõ khuôn mặt, đưa mặt vào khung oval.",
        );
        return;
      }

      const detection = detections[0];
      const steps = stepsForPath(pathRef.current);
      const step = steps[Math.min(checkpointRef.current, steps.length - 1)];
      const pose = poseFromDetection(detection, video);
      const elapsedMs = Date.now() - Date.parse(startedAtRef.current);
      const signature = frameSignature(video);
      const previousPose = lastPoseRef.current;
      const poseJump = previousPose ? poseDistance(pose, previousPose) : 0;
      if (poseJump > POSE_JUMP_WARN) {
        continuityBreaksRef.current += 1;
        stableFramesRef.current = 0;
        if (poseJump > POSE_JUMP_STOP || continuityBreaksRef.current > 1) {
          setError("Phát hiện chuyển động bị nhảy khung. Hãy dùng mặt thật và quay liên tục.");
          stopCamera();
          return;
        }
      }
      lastPoseRef.current = pose;
      sampleResultsRef.current = [
        ...sampleResultsRef.current.slice(-(MAX_SAMPLE_COUNT - 1)),
        {
          elapsedMs,
          detectionScore: detection.detection.score,
          pose,
          frameSignature: signature,
        },
      ];
      const staticError = earlyStaticError(sampleResultsRef.current);
      if (staticError) {
        setError(staticError);
        stopCamera();
        return;
      }

      if (!identityCheckedRef.current) {
        if (identityCheckingRef.current) return;
        if (sampleResultsRef.current.length >= 3) {
          const identityOk = await verifyCurrentIdentity(video);
          if (!identityOk) return;
        } else {
          setLiveStatus("Giữ khuôn mặt thấy rõ để đối chiếu hồ sơ tài khoản...");
          return;
        }
      }

      if (!targetOk(step.target, pose, sideSignRef.current, pathRef.current)) {
        stableFramesRef.current = 0;
        setLiveStatus(statusFor(step.target, pathRef.current));
        return;
      }

      stableFramesRef.current += 1;
      setLiveStatus(`Đúng hướng, tiếp tục chuyển động mượt... (${stableFramesRef.current}/${REQUIRED_STABLE_FRAMES})`);

      if (stableFramesRef.current < REQUIRED_STABLE_FRAMES) return;

      if (step.target === "sideA") {
        sideSignRef.current = Math.sign(pose.yaw) || sideSignRef.current;
      }

      stepResultsRef.current = [
        ...stepResultsRef.current,
        {
          index: checkpointRef.current,
          target: step.target,
          stableFrames: stableFramesRef.current,
          detectionScore: detection.detection.score,
          elapsedMs,
          pose,
          frameSignature: signature,
        },
      ];

      stableFramesRef.current = 0;
      const next = checkpointRef.current + 1;

      if (next >= steps.length) {
        const localError = continuousScanError(sampleResultsRef.current, continuityBreaksRef.current);
        if (localError) {
          setError(localError);
          stopCamera();
          return;
        }
        const [faceDescriptor, profileDescriptors] = await Promise.all([
          descriptorFromVideo(video),
          loadProfileDescriptors(),
        ]);
        if (!faceDescriptor) {
          setError("Chưa lấy được đặc trưng khuôn mặt hiện tại, vui lòng quét lại.");
          stopCamera();
          return;
        }
        if (profileDescriptors.length === 0) {
          setError("Chưa tải được hồ sơ sinh trắc học đã lưu của tài khoản.");
          stopCamera();
          return;
        }
        if (bestFaceDistance(faceDescriptor, profileDescriptors) > FACE_MATCH_THRESHOLD) {
          setError("Khuôn mặt không khớp với hồ sơ sinh trắc học đã lưu của tài khoản.");
          stopCamera();
          return;
        }
        const result: BiometricScanResult = {
          challengeId,
          path: pathRef.current,
          requiredStableFrames: REQUIRED_STABLE_FRAMES,
          startedAt: startedAtRef.current,
          finishedAt: new Date().toISOString(),
          continuityBreaks: continuityBreaksRef.current,
          faceDescriptor,
          profileDescriptors,
          samples: sampleResultsRef.current,
          steps: stepResultsRef.current,
        };
        setScanResult(result);
        checkpointRef.current = next;
        setCheckpoint(next);
        setLiveStatus("Hoàn tất xác thực khuôn mặt.");
        stopCamera();
      } else {
        checkpointRef.current = next;
        setCheckpoint(next);
        setLiveStatus(steps[next].hint);
      }
    } finally {
      isDetectingRef.current = false;
    }
  };

  useEffect(() => {
    if (!open) {
      stopCamera();
      return;
    }

    let cancelled = false;
    checkpointRef.current = 0;
    stableFramesRef.current = 0;
    stepResultsRef.current = [];
    sampleResultsRef.current = [];
    lastPoseRef.current = null;
    continuityBreaksRef.current = 0;
    identityCheckedRef.current = false;
    identityCheckingRef.current = false;
    sideSignRef.current = 0;
    pathRef.current = Math.random() > 0.5 ? "clockwise" : "counterClockwise";
    startedAtRef.current = new Date().toISOString();
    setCheckpoint(0);
    setError("");
    setReady(false);
    setScanResult(null);
    setLiveStatus("Đang tải mô hình nhận diện...");

    loadProfileDescriptors()
      .then(() => {
        if (cancelled) return;
        setLiveStatus("Đang mở camera...");
        return navigator.mediaDevices.getUserMedia({
          video: { width: 640, height: 480, facingMode: "user" },
        });
      })
      .then((stream) => {
        if (!stream) return;
        if (cancelled) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        streamRef.current = stream;
        if (videoRef.current) videoRef.current.srcObject = stream;
      })
      .catch(() => {
        setError("Không thể mở camera hoặc tải mô hình nhận diện.");
      });

    return () => {
      cancelled = true;
      stopCamera();
    };
  }, [open, challengeId, retryNonce]);

  useEffect(() => {
    const stepCount = stepsForPath(pathRef.current).length;
    if (!open || !ready || checkpoint >= stepCount || error) return;
    timerRef.current = window.setInterval(scanCurrentStep, 130);
    return () => {
      if (timerRef.current) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [open, ready, checkpoint, error]);

  if (!open) return null;

  const currentSteps = stepsForPath(pathRef.current);
  const done = checkpoint >= currentSteps.length;
  const progress = Math.min(100, Math.round((checkpoint / currentSteps.length) * 100));
  const currentStep = currentSteps[Math.min(checkpoint, currentSteps.length - 1)];

  return (
    <div className="bio-screen" aria-label="Quét mặt 8D">
      <div className="bio-screen__top">
        <button className="bio-back" onClick={onClose} aria-label="Quay lại">
          &lsaquo;
        </button>
        <div>
          <div className="bio-screen__title">Xác thực khuôn mặt</div>
          <div className="bio-screen__sub">Hãy điều chỉnh mặt theo hướng dẫn bên dưới</div>
        </div>
      </div>

      <div className="bio-face-wrap">
        <div className="bio-face">
          {!done ? (
            <video
              ref={videoRef}
              autoPlay
              muted
              playsInline
              className="bio-face__video"
              onLoadedData={() => {
                setReady(true);
                setLiveStatus(stepsForPath(pathRef.current)[0].hint);
              }}
            />
          ) : (
            <div className="bio-face__done">OK</div>
          )}
        </div>
        <div className="bio-face__ticks" />
        <div className="bio-face__progress" style={{ "--bio-progress": `${progress}%` } as CSSProperties} />
      </div>

      <div className="bio-guide">
        <div className="bio-guide__label">{error || (done ? "Hoàn tất" : currentStep.label)}</div>
        <div className="bio-guide__hint">
          {error || (done ? "Khuôn mặt đã được xác minh. Bấm tiếp tục để chuyển tiền." : liveStatus)}
        </div>
      </div>

      <div className="bio-dots" aria-label={`Tiến độ ${progress}%`}>
        {currentSteps.map((step, index) => (
          <span
            key={step.label}
            className={index < checkpoint || done ? "bio-dots__item bio-dots__item--done" : "bio-dots__item"}
          />
        ))}
      </div>

      <button
        className="btn btn--primary bio-screen__confirm"
        onClick={() => {
          if (error) {
            setRetryNonce((value) => value + 1);
            return;
          }
          if (scanResult) onVerified(scanResult);
        }}
        disabled={!error && (!done || !scanResult)}
      >
        {error ? "Xác thực lại" : "Tiếp tục"}
      </button>
    </div>
  );
};

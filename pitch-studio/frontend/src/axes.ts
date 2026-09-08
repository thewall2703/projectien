import type { AxesResponse } from "./types";

export const AXES: AxesResponse = {
  audience_clusters: [
    { code: "A", label: "Demand", description: "People who might enrol, and the people who decide alongside them" },
    { code: "B", label: "Talent", description: "People who might work here or teach here" },
    { code: "C", label: "Capital", description: "People who might fund the institution or its funds" },
    { code: "D", label: "Commercial", description: "People who might hire from, buy from, or partner with us" },
    { code: "E", label: "Institutional", description: "People who legitimise, regulate, accredit or rank us" },
    { code: "F", label: "Public", description: "People who carry the story onward — press, creators, alumni, social" },
  ],
  durations: [
    { code: "T0", label: "30 seconds", description: "The lift. One sentence plus one proof." },
    { code: "T1", label: "2 minutes", description: "The standard answer." },
    { code: "T2", label: "5 minutes", description: "The coffee conversation." },
    { code: "T3", label: "10 minutes", description: "The seated pitch." },
    { code: "T4", label: "30 minutes", description: "Deck-led session." },
    { code: "T5", label: "90 minutes", description: "Campus walkthrough." },
  ],
  channels: [
    { code: "CH1", label: "Voice only", description: "Phone call. No visuals." },
    { code: "CH2", label: "In person, no assets", description: "Corridor, event floor, dinner table." },
    { code: "CH3", label: "With deck", description: "In person or video call, with a deck." },
    { code: "CH4", label: "Campus walkthrough", description: "Physical space does the proving." },
    { code: "CH5", label: "Written", description: "Email, WhatsApp, LinkedIn." },
    { code: "CH6", label: "Stage", description: "Podium, panel, conference." },
    { code: "CH7", label: "Camera", description: "Recorded or broadcast." },
  ],
  intents: [
    { code: "I1", label: "Inform", description: "They do not know what this is." },
    { code: "I2", label: "Persuade", description: "They know what it is and are deciding." },
    { code: "I3", label: "Defend", description: "They have an objection, spoken or unspoken." },
    { code: "I4", label: "Recruit", description: "We want them to join." },
    { code: "I5", label: "Transact", description: "We want them to buy, partner, hire or fund." },
    { code: "I6", label: "Represent", description: "Formal or institutional. We are on the record." },
  ],
  temperatures: [
    { code: "X1", label: "Cold", description: "Never heard of us." },
    { code: "X2", label: "Warm", description: "Heard of us. Curious, possibly sceptical." },
    { code: "X3", label: "Hot", description: "Actively evaluating." },
    { code: "X4", label: "Closing", description: "Ready, but has one blocker left." },
    { code: "X5", label: "Post-decision", description: "Already in. Now needs to tell the story." },
  ],
};

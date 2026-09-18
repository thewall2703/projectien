import { useNavigate } from "react-router-dom";
import { api } from "../api";
import GenerateWizard, { type GenerateWizardPayload } from "../components/generate/GenerateWizard";
import type { Generation } from "../types";

export default function Generate() {
  const navigate = useNavigate();

  const onSubmit = async (payload: GenerateWizardPayload) => {
    const generation = (await api.createGeneration(payload)) as Generation;
    navigate(`/result/${generation.id}`);
  };

  return <GenerateWizard kicker="Generate" submitLabel="Generate now" onSubmit={onSubmit} />;
}

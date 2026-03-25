# Preguntas de Admisión — Español

Preguntas organizadas por tipo de caso. Haga una a la vez. Adapte el seguimiento según la respuesta.

---

## Preguntas Universales (todos los tipos de caso)
Haga estas antes de las preguntas específicas por tipo de caso.

| Prioridad | Pregunta | Campo |
|---|---|---|
| 1 | "¿Cuánto tiempo lleva viviendo en los Estados Unidos?" | `years_in_us` |
| 2 | "¿Cómo entró a los Estados Unidos? ¿Por ejemplo, con una visa, en la frontera, u otra manera?" | `entry_method` |
| 3 | "¿Tiene algún familiar inmediato que sea ciudadano americano o residente permanente (tarjeta verde)?" | `us_family_connections` |

---

## Peticiones Familiares

| Prioridad | Pregunta | Campo | Notas |
|---|---|---|---|
| 1 | "¿A quién espera patrocinar, o quién le está patrocinando a usted?" | `petitioner_relationship` | Cónyuge, padre, hijo, hermano |
| 2 | "¿El familiar que está en los Estados Unidos es ciudadano americano o tiene tarjeta verde?" | `petitioner_status` | |
| 3 | "¿Cuál es su estado civil?" | `marital_status` | |
| 4 | "¿Tiene hijos que también vendrían a los Estados Unidos?" | `num_dependents` | |
| 5 | "¿Alguno de sus hijos está actualmente en los Estados Unidos?" | `dependents_in_us` | |

---

## Visas de Trabajo / Empleo

| Prioridad | Pregunta | Campo | Notas |
|---|---|---|---|
| 1 | "¿Tiene actualmente un empleador que esté dispuesto a patrocinar su visa?" | `employer_willing_to_sponsor` | |
| 2 | "¿En qué trabaja y cuál es su cargo o puesto?" | `job_title` | |
| 3 | "¿Cuál es el nivel más alto de educación que completó?" | `education_level` | |
| 4 | "¿Cuántos años de experiencia tiene en su área de trabajo?" | `years_experience` | |
| 5 | "¿Cómo se llama la empresa o empleador interesado en patrocinarle?" | `employer_name` | Omitir si no hay empleador aún |

---

## Asilo

| Prioridad | Pregunta | Campo | Notas |
|---|---|---|---|
| 1 | "¿Cuándo llegó a los Estados Unidos?" | `arrival_date_us` | Verificar plazo de 1 año |
| 2 | "¿Ya ha solicitado asilo, o es la primera vez que contacta a un abogado sobre esto?" | `has_filed_asylum` | |
| 3 | "¿De qué país es usted?" | `country_of_persecution` | |
| 4 | "¿Puede decirme en términos generales por qué salió de su país? ¿Por ejemplo, fue por su religión, sus ideas políticas, o algo relacionado con su identidad?" | `persecution_type` | Solo categoría general — NO pedir narrativa detallada |

> Nota: Nunca solicite narrativas detalladas de abuso o trauma. Recopile solo la categoría general (religión, nacionalidad, opinión política, grupo social, raza). Registrar para el abogado.

---

## Deportación / Remoción

| Prioridad | Pregunta | Campo | Notas |
|---|---|---|---|
| 1 | "¿Ha recibido un documento oficial llamado 'Aviso de Comparecencia' o NTA?" | `has_nta` | |
| 2 | "¿Tiene una fecha programada para una audiencia ante la corte de inmigración?" | `has_court_date`, `court_date` | |
| 3 | "¿Dónde está ubicada su corte de inmigración?" | `court_location` | |
| 4 | "¿Ha sido deportado o removido de los Estados Unidos alguna vez?" | `prior_deportation` | |
| 5 | "¿Está actualmente detenido en un centro de detención migratoria?" | `is_detained` | Ya se preguntó en el triaje de urgencia |

---

## DACA

| Prioridad | Pregunta | Campo | Notas |
|---|---|---|---|
| 1 | "¿Cuándo vence su DACA actual?" | `visa_expiration_date` | |
| 2 | "¿Ha habido algún cambio en su situación desde su última renovación? ¿Por ejemplo, nueva dirección, viajes fuera de los Estados Unidos, algún problema legal?" | `extra_data.daca_notes` | |
| 3 | "¿En qué año llegó por primera vez a los Estados Unidos?" | Inferir `years_in_us` | |

---

## Naturalización / Ciudadanía

| Prioridad | Pregunta | Campo | Notas |
|---|---|---|---|
| 1 | "¿Desde hace cuánto tiempo tiene su tarjeta verde (residencia permanente)?" | Inferir de `years_in_us` | |
| 2 | "¿Ha viajado fuera de los Estados Unidos por más de 6 meses seguidos en los últimos 5 años?" | `extra_data.long_trips_abroad` | |
| 3 | "¿Ha tenido algún problema legal o ha sido arrestado en los Estados Unidos o en otro país?" | `has_criminal_record` | Solo sí/no |

---

## Lista de Documentos (todos los tipos de caso, Nivel 3)
Preguntar solo si el llamante tiene tiempo después de las preguntas específicas del caso.

> "Para terminar, le haré una lista rápida — ¿tiene alguno de los siguientes documentos disponibles? Sí o no está bien."

| Documento | `document_type` |
|---|---|
| Pasaporte vigente | `passport` |
| Acta de nacimiento | `birth_certificate` |
| Sellos de visa o entradas a los EE. UU. | `visa_stamps` |
| Registro I-94 de viaje | `i94` |
| Prueba de vínculos en EE. UU. (contrato de arrendamiento, declaración de impuestos, carta de empleo) | `employment_letter`, `tax_returns` |
| Acta de matrimonio (si aplica) | `marriage_certificate` |

---

## Manejo de Temas Sensibles

**Antecedentes penales:**
- Preguntar solo sí/no: "¿Ha sido arrestado alguna vez por o condenado por algún delito en los Estados Unidos o en otro país?"
- NO preguntar detalles, cargos, fechas ni resultados — eso es para el abogado
- Registrar solo `has_criminal_record = TRUE/FALSE`

**Negativas de visa previas:**
- Preguntar solo sí/no: "¿Alguna vez le han negado una visa o rechazado una solicitud de inmigración?"
- NO preguntar detalles — el abogado revisará

**Fraude migratorio previo:**
- Si el llamante ofrece esta información voluntariamente, decir: "Aprecio su honestidad. Por favor comparta esos detalles con el abogado — está en la mejor posición para asesorarle de manera confidencial."
- NO registrar los detalles en la ficha de admisión

---

## Frases para Saltar Preguntas con Gracia

Use estas cuando el llamante no sabe o no quiere responder:

- "No hay problema, el abogado lo revisará en su consulta."
- "Está bien — déjeme tomar nota de que necesitamos discutir eso."
- "No necesita tener esa información en este momento."
